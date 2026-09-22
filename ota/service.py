"""OTA 核心服务：设备、固件、发布、批次、回滚与看门狗全部规则的唯一入口。

并发模型：
  * store.lock（RLock）串行化所有复合状态迁移；每个公开方法在一个临界区内
    完成“校验→迁移→计数→落盘”，并发请求结果一致、不会重复计数或状态倒退。
  * 设备级 in-flight 标记 _rolling_back 用于快速区分“正在回滚”并发冲突，
    真正的权威状态仍是任务本身（重启后从快照重建标记）。

顺序模型（乱序/迟到上报如何收敛）：
  * 心跳带单调 seq，seq 不增即拒绝（StaleHeartbeat，区分 duplicate/regressed）。
  * 任务阶段带单调 stage_seq，设备每次上报自报 seq：seq 更小=迟到丢弃；
    seq 相同=重复，幂等返回但不重复计数；阶段还必须等于当前阶段。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from .errors import (
    BatchExhausted,
    DeviceAlreadyRegistered,
    DeviceNotFound,
    DuplicateReport,
    FirmwareAlreadyExists,
    FirmwareNotFound,
    IncompatibleFirmware,
    InvalidDeviceIdentity,
    InvalidStageTransition,
    InvalidUpgradePath,
    JobNotFound,
    NoEligibleDevice,
    ReleaseNotFound,
    ReleaseStateConflict,
    RollbackInProgress,
    RollbackNotAllowed,
    RollbackTargetMissing,
    StaleHeartbeat,
    StaleReport,
)
from .models import (
    STAGE_ORDER,
    TERMINAL_STAGES,
    AuditEvent,
    Batch,
    BatchState,
    Device,
    DeviceStatus,
    Firmware,
    Job,
    JobDirection,
    JobStage,
    Release,
    ReleaseState,
)
from .persistence import JsonStore
from .clock import Clock

log = logging.getLogger("ota")

_PBKDF2_ROUNDS = 120_000


def parse_version(text: str) -> tuple[int, ...]:
    """把 '1.2.10' 解析为可比较元组；非法格式抛 ValueError。"""
    parts = text.strip().split(".")
    if not parts or any(not p.isdigit() for p in parts):
        raise ValueError(f"invalid version: {text!r}")
    return tuple(int(p) for p in parts)


def _hash_token(device_id: str, token: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", token.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return salt.hex(), digest.hex()


class OtaService:
    def __init__(self, store: JsonStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or Clock()
        # 设备级“回滚在途”标记：仅快速区分并发回滚冲突。
        # 所有读写都在 store.lock 临界区内，因此无需第二把锁，
        # 也不存在锁顺序死锁；权威状态仍是 Job（重启后据此重建标记）。
        self._rolling_back: set[str] = set()
        store.load()
        self._rebuild_inflight()

    def _rebuild_inflight(self) -> None:
        with self.store.lock:
            for job in self.store.jobs.values():
                if (
                    job.direction is JobDirection.ROLLBACK
                    and job.stage not in TERMINAL_STAGES
                ):
                    self._rolling_back.add(job.device_id)

    # ------------------------------------------------------------ 审计
    def _audit(self, kind: str, ref: str, message: str, **details: Any) -> None:
        evt = AuditEvent(
            ts=self.clock.now(), kind=kind, ref=ref, message=message, details=details
        )
        data = evt.to_dict()
        self.store.audit.append(data)
        log.info("AUDIT kind=%s ref=%s msg=%s details=%s", kind, ref, message, details)

    # ==================================================================
    # 设备注册与心跳
    # ==================================================================
    def register_device(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id") or "").strip()
        model = str(payload.get("model") or "").strip()
        hw_version = str(payload.get("hw_version") or "").strip()
        fw_version = str(payload.get("current_fw_version") or "").strip()
        if not device_id or not model or not hw_version or not fw_version:
            raise InvalidDeviceIdentity(
                "device_id/model/hw_version/current_fw_version 均不能为空",
                reason="missing_identity_field",
            )
        try:
            parse_version(fw_version)
        except ValueError:
            raise InvalidDeviceIdentity(
                f"current_fw_version 非法: {fw_version!r}", reason="bad_version"
            )
        now = self.clock.now()
        with self.store.lock:
            existing = self.store.devices.get(device_id)
            if existing is not None:
                # 重复注册绝不静默覆盖：校验是否为“同一设备的重复登记”
                same = (
                    existing.model == model
                    and existing.hw_version == hw_version
                )
                reason = "identity_conflict" if same else "identity_tampered"
                self._audit(
                    "register_rejected", device_id,
                    f"重复注册被拒绝({reason})",
                    incoming={"model": model, "hw_version": hw_version},
                    existing={"model": existing.model, "hw_version": existing.hw_version},
                )
                raise DeviceAlreadyRegistered(
                    f"设备 {device_id} 已注册，重复注册不允许覆盖既有状态",
                    reason=reason,
                )
            # 设备令牌只生成一次：返回明文，库存哈希（salt:hash）
            token = f"tok-{device_id}-{os.urandom(8).hex()}"
            salt_hex, hash_hex = _hash_token(device_id, token)
            dev = Device(
                device_id=device_id,
                model=model,
                hw_version=hw_version,
                token_hash=f"{salt_hex}${hash_hex}",
                current_fw_version=fw_version,
                status=DeviceStatus.REGISTERED,
                registered_ts=now,
            )
            self.store.devices[device_id] = dev
            self.store.save()
        log.info("INPUT register device=%s model=%s hw=%s fw=%s -> 已注册并签发令牌",
                 device_id, model, hw_version, fw_version)
        self._audit("device_registered", device_id, "设备注册成功", model=model,
                    hw_version=hw_version, fw_version=fw_version)
        return {"device": dev.to_dict(), "token": token}

    def heartbeat(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "")
        seq = payload.get("seq")
        if not device_id or not token:
            raise InvalidDeviceIdentity("device_id/token 不能为空",
                                        reason="missing_credentials")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise InvalidDeviceIdentity("心跳 seq 必须为非负整数", reason="bad_seq")
        with self.store.lock:
            dev = self.store.devices.get(device_id)
            if dev is None:
                raise DeviceNotFound(f"设备 {device_id} 不存在", reason="unknown_device")
            self._verify_token(dev, token)
            now = self.clock.now()
            if seq < dev.last_heartbeat_seq:
                self._audit("heartbeat_rejected", device_id,
                            f"过期心跳被拒绝 seq={seq} < 已接受 seq={dev.last_heartbeat_seq}",
                            seq=seq, accepted_seq=dev.last_heartbeat_seq)
                raise StaleHeartbeat(
                    f"心跳序号回退: {seq} < {dev.last_heartbeat_seq}",
                    reason="seq_regressed",
                )
            if seq == dev.last_heartbeat_seq and dev.last_heartbeat_ts is not None:
                self._audit("heartbeat_rejected", device_id,
                            f"重复心跳被拒绝 seq={seq}", seq=seq)
                raise StaleHeartbeat(
                    f"心跳序号重复: {seq}", reason="seq_duplicate"
                )
            prev_status = dev.status
            dev.last_heartbeat_seq = seq
            dev.last_heartbeat_ts = now
            dev.status = DeviceStatus.ONLINE
            self.store.save()
            # 重连补齐：设备在线后，其 SUSPENDED 看门狗任务可恢复（见 sweep 规则）
            resumed = self._resume_suspended_jobs(device_id, now)
        log.info("INPUT heartbeat device=%s seq=%s -> %s；恢复搁置任务=%s",
                 device_id, seq, prev_status.value, resumed)
        return {"device_id": device_id, "status": dev.status.value, "seq": seq,
                "resumed_jobs": resumed}

    @staticmethod
    def _verify_token(dev: Device, token: str) -> None:
        try:
            salt_hex, hash_hex = dev.token_hash.split("$", 1)
        except ValueError:
            raise InvalidDeviceIdentity("设备凭据存储损坏", reason="bad_credential_store")
        _, calc = _hash_token(dev.device_id, token, bytes.fromhex(salt_hex))
        if not hmac.compare_digest(calc, hash_hex):
            raise InvalidDeviceIdentity("设备令牌伪造或不匹配", reason="bad_token")

    # ==================================================================
    # 固件
    # ==================================================================
    def create_firmware(self, payload: dict) -> dict:
        fw_id = str(payload.get("fw_id") or "").strip()
        version = str(payload.get("version") or "").strip()
        model = str(payload.get("model") or "").strip()
        hw_version = str(payload.get("hw_version") or "").strip()
        checksum = str(payload.get("checksum") or "").strip()
        if not fw_id or not version or not model or not hw_version or not checksum:
            raise InvalidDeviceIdentity("固件字段不完整", reason="missing_firmware_field")
        try:
            parse_version(version)
        except ValueError:
            raise InvalidDeviceIdentity(f"固件版本非法: {version!r}", reason="bad_version")
        now = self.clock.now()
        with self.store.lock:
            if fw_id in self.store.firmwares:
                self._audit("firmware_rejected", fw_id, "固件 ID 重复", version=version)
                raise FirmwareAlreadyExists(f"固件 {fw_id} 已存在", reason="fw_id_exists")
            for other in self.store.firmwares.values():
                if (other.model == model and other.hw_version == hw_version
                        and other.version == version):
                    self._audit("firmware_rejected", fw_id,
                                "同型号同硬件版本号重复", version=version)
                    raise FirmwareAlreadyExists(
                        f"{model}/{hw_version} 的 {version} 固件已存在",
                        reason="version_exists",
                    )
            fw = Firmware(
                fw_id=fw_id, version=version, model=model, hw_version=hw_version,
                checksum=checksum, created_ts=now,
            )
            self.store.firmwares[fw_id] = fw
            self.store.save()
        log.info("INPUT firmware id=%s %s/%s v%s -> 已创建", fw_id, model, hw_version, version)
        self._audit("firmware_created", fw_id, "固件创建成功", version=version,
                    model=model, hw_version=hw_version)
        return {"firmware": fw.to_dict()}

    # ==================================================================
    # 发布与批次
    # ==================================================================
    def create_release(self, payload: dict) -> dict:
        target_fw_id = str(payload.get("target_fw_id") or "").strip()
        name = str(payload.get("name") or target_fw_id).strip()
        batch_size = int(payload.get("batch_size", 5))
        threshold = float(payload.get("failure_threshold", 0.1))
        max_stage_attempts = int(payload.get("max_stage_attempts", 3))
        stage_timeout = float(payload.get("stage_timeout", 300.0))
        heartbeat_timeout = float(payload.get("heartbeat_timeout", 120.0))
        allow_downgrade = bool(payload.get("allow_downgrade", False))
        allowed_from = set(payload.get("allowed_from_versions") or [])
        if batch_size < 1:
            raise ReleaseStateConflict("batch_size 必须 >=1", reason="bad_batch_size")
        if not 0.0 <= threshold <= 1.0:
            raise ReleaseStateConflict("failure_threshold 必须在 [0,1]",
                                       reason="bad_threshold")
        if max_stage_attempts < 1 or stage_timeout <= 0 or heartbeat_timeout <= 0:
            raise ReleaseStateConflict("超时/重试参数非法", reason="bad_timeout_config")
        for v in allowed_from:
            try:
                parse_version(v)
            except ValueError:
                raise ReleaseStateConflict(f"allowed_from_versions 含非法版本 {v!r}",
                                           reason="bad_version")
        with self.store.lock:
            fw = self.store.firmwares.get(target_fw_id)
            if fw is None:
                raise FirmwareNotFound(f"目标固件 {target_fw_id} 不存在",
                                       reason="target_fw_missing")
            if fw.yanked:
                raise InvalidUpgradePath("目标固件已被撤下", reason="firmware_yanked")
            now = self.clock.now()
            release_id = self.store.next_id("release")
            rel = Release(
                release_id=release_id, target_fw_id=target_fw_id, name=name,
                batch_size=batch_size, failure_threshold=threshold,
                max_stage_attempts=max_stage_attempts, stage_timeout=stage_timeout,
                heartbeat_timeout=heartbeat_timeout,
                allow_downgrade=allow_downgrade,
                allowed_from_versions=allowed_from,
                state=ReleaseState.PENDING, created_ts=now,
            )
            self.store.releases[release_id] = rel
            self.store.save()
        log.info("INPUT release id=%s target=%s size=%s threshold=%s -> 已创建(PENDING)",
                 release_id, target_fw_id, batch_size, threshold)
        self._audit("release_created", release_id, "发布任务创建",
                    target_fw_id=target_fw_id, batch_size=batch_size,
                    failure_threshold=threshold)
        return {"release": rel.to_dict()}

    def start_release(self, release_id: str) -> dict:
        with self.store.lock:
            rel = self._get_release(release_id)
            if rel.state is not ReleaseState.PENDING:
                raise ReleaseStateConflict(
                    f"发布当前状态 {rel.state.value}，只有 PENDING 可开始",
                    reason=f"cannot_start_from_{rel.state.value}")
            rel.state = ReleaseState.ACTIVE
            self.store.save()
        self._audit("release_started", release_id, "发布开始")
        return {"release": rel.to_dict()}

    def pause_release(self, release_id: str) -> dict:
        with self.store.lock:
            rel = self._get_release(release_id)
            if rel.state is not ReleaseState.ACTIVE:
                raise ReleaseStateConflict(
                    f"只有 ACTIVE 可暂停，当前 {rel.state.value}",
                    reason=f"cannot_pause_from_{rel.state.value}")
            rel.state = ReleaseState.PAUSED
            self.store.save()
        self._audit("release_paused", release_id, "发布人工暂停（不再规划新批次）")
        return {"release": rel.to_dict()}

    def resume_release(self, release_id: str) -> dict:
        with self.store.lock:
            rel = self._get_release(release_id)
            if rel.state is not ReleaseState.PAUSED:
                raise ReleaseStateConflict(
                    f"只有 PAUSED 可继续，当前 {rel.state.value}",
                    reason=f"cannot_resume_from_{rel.state.value}")
            rel.state = ReleaseState.ACTIVE
            rel.halt_reason = None
            self.store.save()
        self._audit("release_resumed", release_id, "发布继续")
        return {"release": rel.to_dict()}

    def abort_release(self, release_id: str) -> dict:
        """人工终止：未开始/未认领的设备搁置，已下发的继续收敛到终态。"""
        with self.store.lock:
            rel = self._get_release(release_id)
            if rel.state in (ReleaseState.COMPLETED, ReleaseState.ABORTED):
                raise ReleaseStateConflict(
                    f"发布已 {rel.state.value}，不能终止",
                    reason=f"cannot_abort_from_{rel.state.value}")
            rel.state = ReleaseState.ABORTED
            rel.halt_reason = "manual_abort"
            for b in self._batches_of(release_id):
                if b.state is BatchState.ACTIVE:
                    b.state = BatchState.STOPPED
            now = self.clock.now()
            for job in self._jobs_of(release_id):
                if job.stage is JobStage.PENDING:
                    self._terminate_job(
                        job, JobStage.SUSPENDED, "release_aborted_pending", now)
            self.store.save()
        self._audit("release_aborted", release_id, "发布人工终止")
        return {"release": rel.to_dict()}

    def plan_next_batch(self, release_id: str) -> dict:
        with self.store.lock:
            rel = self._get_release(release_id)
            if rel.state is not ReleaseState.ACTIVE:
                raise ReleaseStateConflict(
                    f"只有 ACTIVE 发布可规划下一批，当前 {rel.state.value}",
                    reason=f"cannot_plan_from_{rel.state.value}")
            target = self.store.firmwares[rel.target_fw_id]
            enrolled = {
                j.device_id
                for j in self.store.jobs.values()
                if j.release_id == release_id
            }
            candidates: list[Device] = []
            skipped: dict[str, str] = {}
            for dev in sorted(self.store.devices.values(), key=lambda d: d.device_id):
                if dev.device_id in enrolled:
                    continue
                reason = self._eligibility_reason(dev, target, rel)
                if reason:
                    skipped[dev.device_id] = reason
                    continue
                candidates.append(dev)
            for did, why in skipped.items():
                self._audit("device_skipped", did,
                            f"设备不纳入发布 {release_id}: {why}",
                            release_id=release_id, reason=why)
            if not candidates:
                self._audit("plan_rejected", release_id,
                            "没有符合条件的设备可纳入下一批", skipped=len(skipped))
                raise NoEligibleDevice(
                    "没有型号/硬件/版本/升级路径均满足的设备",
                    reason="no_candidate",
                )
            chosen = candidates[: rel.batch_size]
            ordinal = len(self._batches_of(release_id)) + 1
            batch_id = self.store.next_id("batch")
            now = self.clock.now()
            batch = Batch(
                batch_id=batch_id, release_id=release_id, ordinal=ordinal,
                device_ids=[d.device_id for d in chosen],
                state=BatchState.ACTIVE,
            )
            self.store.batches[batch_id] = batch
            new_jobs: list[Job] = []
            for dev in chosen:
                job = Job(
                    job_id=self.store.next_id("job"),
                    release_id=release_id, batch_id=batch_id,
                    device_id=dev.device_id, target_fw_id=target.fw_id,
                    from_version=dev.current_fw_version,
                    direction=JobDirection.UPGRADE, stage=JobStage.PENDING,
                    stage_seq=0, attempts=0, created_ts=now,
                    stage_entered_ts=now, last_progress_ts=now,
                )
                self.store.jobs[job.job_id] = job
                new_jobs.append(job)
            self.store.save()
        log.info("INPUT plan release=%s -> 批次 %s 纳入设备 %s",
                 release_id, batch_id, batch.device_ids)
        self._audit("batch_planned", batch_id,
                    f"批次 {ordinal} 已规划", release_id=release_id,
                    devices=batch.device_ids)
        return {"batch": batch.to_dict(),
                "jobs": [j.to_dict() for j in new_jobs],
                "skipped": skipped}

    def claim_job(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "")
        with self.store.lock:
            dev = self._get_device(device_id)
            self._verify_token(dev, token)
            now = self.clock.now()
            # 同一设备只可能有一个非终态任务（升级或回滚）
            active_jobs = [
                j for j in self.store.jobs.values()
                if j.device_id == device_id and j.stage not in TERMINAL_STAGES
            ]
            pending = [j for j in active_jobs if j.stage is JobStage.PENDING]
            if not pending:
                if active_jobs:
                    j = active_jobs[0]
                    # 重复认领已经在执行的任务：幂等返回，不重复计数
                    return {"job": j.to_dict(), "duplicate": True}
                raise BatchExhausted("设备没有可认领的任务", reason="nothing_to_claim")
            # 确定性选择：按 job_id（即创建顺序）取最旧
            job = sorted(pending, key=lambda j: j.job_id)[0]
            rel = self._get_release(job.release_id)
            batch = self.store.batches[job.batch_id]
            if rel.state in (ReleaseState.ABORTED,):
                self._terminate_job(job, JobStage.SUSPENDED, "release_aborted", now)
                self.store.save()
                raise BatchExhausted("发布已终止，任务不可认领",
                                     reason="release_aborted")
            if batch.state is BatchState.STOPPED:
                self._terminate_job(job, JobStage.SUSPENDED, "batch_stopped", now)
                self.store.save()
                raise BatchExhausted("批次已停止，任务不可认领", reason="batch_stopped")
            # CAS：PENDING→DOWNLOADING 本身即认领令牌；store 锁串行化保证
            # 并发认领只有一个请求能完成迁移，后来者走上面的“执行中幂等返回”。
            # claimed 仅用于批次可见性（回滚任务复用批次时不会阻塞新任务认领）。
            batch.claimed.add(device_id)
            self._advance(job, JobStage.DOWNLOADING, now, attempts=0)
            self.store.save()
            log.info("CLAIM device=%s job=%s PENDING->DOWNLOADING (batch=%s)",
                     device_id, job.job_id, batch.batch_id)
            return {"job": job.to_dict(), "duplicate": False}

    # ==================================================================
    # 阶段上报
    # ==================================================================
    def report_stage(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "")
        job_id = str(payload.get("job_id") or "").strip()
        stage = payload.get("stage")
        seq = payload.get("seq")
        failed = bool(payload.get("failed", False))
        reason = str(payload.get("reason") or "")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise InvalidDeviceIdentity("上报 seq 必须为非负整数", reason="bad_seq")
        with self.store.lock:
            dev = self._get_device(device_id)
            self._verify_token(dev, token)
            job = self.store.jobs.get(job_id)
            if job is None or job.device_id != device_id:
                raise JobNotFound(f"任务 {job_id} 不属于该设备", reason="job_missing")
            now = self.clock.now()
            try:
                new_stage = JobStage(stage)
            except ValueError:
                raise InvalidStageTransition(f"未知阶段 {stage!r}", reason="unknown_stage")

            # ---- 迟到/重复上报收敛规则 ----
            if seq < job.last_report_seq:
                self._audit("report_rejected", job_id,
                            f"迟到上报被丢弃 seq={seq} < {job.last_report_seq}",
                            device_id=device_id, stage=stage)
                raise StaleReport(
                    f"上报序号 {seq} 落后于已接受 {job.last_report_seq}",
                    reason="seq_regressed")
            if seq == job.last_report_seq:
                # 完全重复：幂等返回当前状态，绝不重复计数/重试
                self._audit("report_duplicate", job_id,
                            f"重复上报幂等忽略 seq={seq} stage={stage}",
                            device_id=device_id)
                raise DuplicateReport(
                    "重复上报已幂等忽略", reason="seq_duplicate")

            if job.stage in TERMINAL_STAGES:
                # 终态后到达的更高 seq 上报：不允许复活任务
                self._audit("report_rejected", job_id,
                            f"任务已终态 {job.stage.value}，拒绝迁移到 {new_stage.value}",
                            device_id=device_id)
                raise InvalidStageTransition(
                    f"任务已终态({job.stage.value})，不可再迁移",
                    reason="terminal_immutable")

            # ---- 失败上报：任何活动阶段都可失败，进入 FAILED 终态 ----
            if failed:
                # 只有真正被接受的上报才推进 seq / 迁移状态
                job.last_report_seq = seq
                self._terminate_job(job, JobStage.FAILED,
                                    reason or "device_reported_failure", now)
                self._after_terminal(job)
                self.store.save()
                log.info("REPORT device=%s job=%s -> FAILED(%s)",
                         device_id, job_id, reason)
                return {"job": job.to_dict()}

            # ---- 正常阶段推进：只允许前进到“下一个”阶段 ----
            expected = self._next_stage(job.stage)
            if new_stage == job.stage:
                # 反复上报同一阶段：仅在“超过阶段超时且仍有重试额度”时消耗重试，
                # 此时该上报被视为一次有效的看门狗重试信号，接受其 seq；
                # 否则拒绝且不消费 seq（后续正确上报仍可被接受）。
                rel = self._get_release(job.release_id)
                if (now - job.stage_entered_ts > rel.stage_timeout
                        and job.attempts + 1 < rel.max_stage_attempts):
                    job.last_report_seq = seq
                    job.attempts += 1
                    job.last_progress_ts = now
                    self.store.save()
                    self._audit("stage_retry", job.job_id,
                                f"设备反复上报 {new_stage.value}，消耗一次重试",
                                device_id=device_id, attempts=job.attempts)
                    raise InvalidStageTransition(
                        f"重复阶段 {new_stage.value}，已记录第 {job.attempts} 次重试",
                        reason="same_stage_repeated_retry")
                self._audit("report_rejected", job_id,
                            f"反复上报同一阶段 {new_stage.value}，等待看门狗处置",
                            device_id=device_id, attempts=job.attempts)
                raise InvalidStageTransition(
                    f"阶段 {new_stage.value} 已在进行中，需推进到 {expected.value}",
                    reason="same_stage_repeated")
            if new_stage != expected:
                # 非法阶段（跳阶段/倒退）：拒绝且不消费 seq，
                # 保证迟到/乱序上报的判定始终基于“已接受的最大 seq”
                self._audit("report_rejected", job_id,
                            f"非法阶段迁移 {job.stage.value}->{new_stage.value}，"
                            f"期望 {expected.value}",
                            device_id=device_id)
                raise InvalidStageTransition(
                    f"只能从 {job.stage.value} 推进到 {expected.value}，"
                    f"收到 {new_stage.value}",
                    reason="non_adjacent_or_backward")

            # 全部校验通过：此刻才接受 seq 并迁移
            job.last_report_seq = seq
            self._advance(job, new_stage, now, attempts=0)
            # CONFIRMED / ROLLED_BACK 都是成功终态（方向不同）
            if new_stage in (JobStage.CONFIRMED, JobStage.ROLLED_BACK):
                self._on_success(job, now)
                self._after_terminal(job)
            self.store.save()
            self._audit("stage_advanced", job_id,
                        f"阶段推进至 {new_stage.value}",
                        device_id=device_id, seq=seq,
                        direction=job.direction.value)
        log.info("REPORT device=%s job=%s -> %s seq=%s",
                 device_id, job_id, new_stage.value, seq)
        return {"job": job.to_dict()}

    # ==================================================================
    # 回滚
    # ==================================================================
    def rollback_device(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id") or "").strip()
        target_fw_id = str(payload.get("target_fw_id") or "").strip()
        try:
            with self.store.lock:
                dev = self._get_device(device_id)
                now = self.clock.now()
                if device_id in self._rolling_back:
                    raise RollbackInProgress("该设备已有回滚任务在执行中",
                                             reason="rollback_inflight")
                # _resolve 抛错时不会加标记，直接传播并带可区分原因
                target = self._resolve_rollback_target(dev, target_fw_id,
                                                       exclude_unfinished=True)
                self._rolling_back.add(device_id)
                job = self._create_rollback_job(dev, target, now, reason="manual")
                self.store.save()
        except (RollbackInProgress, RollbackNotAllowed, RollbackTargetMissing) as exc:
            # 所有回滚拒绝都必须留下可追溯记录
            self._audit("rollback_rejected", device_id,
                        f"单设备回滚被拒绝: {exc.message}", reason=exc.reason)
            raise
        log.info("ROLLBACK device=%s -> fw=%s job=%s 已创建",
                 device_id, target.fw_id, job.job_id)
        self._audit("rollback_created", job.job_id,
                    f"设备 {device_id} 回滚任务创建", device_id=device_id,
                    target_fw_id=target.fw_id)
        return {"job": job.to_dict()}

    def rollback_batch(self, release_id: str, batch_id: str,
                       target_fw_id: str | None) -> dict:
        """整批回滚：目标统一；不满足条件的设备逐一记录拒绝原因，绝不半更新。"""
        target_fw_id = (target_fw_id or "").strip()
        created: list[str] = []
        rejected: dict[str, str] = {}
        with self.store.lock:
            rel = self._get_release(release_id)
            batch = self.store.batches.get(batch_id)
            if batch is None or batch.release_id != release_id:
                raise ReleaseNotFound(f"批次 {batch_id} 不属于发布 {release_id}",
                                      reason="batch_not_found")
            now = self.clock.now()
            target: Firmware | None = None
            if target_fw_id:
                target = self.store.firmwares.get(target_fw_id)
                if target is None:
                    raise RollbackTargetMissing(
                        f"回滚目标固件 {target_fw_id} 不存在",
                        reason="target_fw_missing")
            for device_id in batch.device_ids:
                dev = self.store.devices[device_id]
                # 检查与建单在同一临界区，杜绝“检查通过后被并发抢先”窗口
                if device_id in self._rolling_back:
                    rejected[device_id] = "rollback_inflight"
                    self._audit("rollback_rejected", device_id,
                                "整批回滚：已有回滚在执行", batch_id=batch_id)
                    continue
                try:
                    tgt = self._resolve_rollback_target(
                        dev, target.fw_id if target else "", exclude_unfinished=True)
                except Exception as exc:  # noqa: BLE001 - 逐设备归类
                    code = getattr(exc, "reason", exc.__class__.__name__)
                    rejected[device_id] = code
                    self._audit("rollback_rejected", device_id,
                                f"整批回滚被拒绝: {exc}", batch_id=batch_id,
                                reason=code)
                    continue
                self._rolling_back.add(device_id)
                job = self._create_rollback_job(dev, tgt, now, reason="batch",
                                                batch_id=batch_id)
                created.append(job.job_id)
            self.store.save()
        log.info("ROLLBACK-BATCH release=%s batch=%s created=%s rejected=%s",
                 release_id, batch_id, created, rejected)
        self._audit("rollback_batch", batch_id,
                    f"整批回滚完成建单：成功 {len(created)}，拒绝 {len(rejected)}",
                    release_id=release_id, created=created, rejected=rejected)
        return {"created_jobs": created, "rejected": rejected}

    # ==================================================================
    # 看门狗
    # ==================================================================
    def sweep(self) -> dict:
        """周期性维护：心跳超时置离线；活动任务阶段超时则重试或搁置。

        不触碰 PENDING（设备尚未开始）与终态任务，因此个别卡住的设备
        不会阻塞整个批次；搁置设备重连上报后可恢复。
        """
        offline: list[str] = []
        retried: list[str] = []
        suspended: list[str] = []
        with self.store.lock:
            now = self.clock.now()
            rel_map = self.store.releases
            for dev in self.store.devices.values():
                if dev.status is not DeviceStatus.ONLINE:
                    continue
                if dev.last_heartbeat_ts is None:
                    continue
                rel_id = self._release_of_device(dev.device_id)
                timeout = (
                    rel_map[rel_id].heartbeat_timeout
                    if rel_id is not None else 120.0
                )
                if now - dev.last_heartbeat_ts > timeout:
                    dev.status = DeviceStatus.OFFLINE
                    offline.append(dev.device_id)
            for job in self.store.jobs.values():
                if job.stage in TERMINAL_STAGES or job.stage is JobStage.PENDING:
                    continue
                rel = rel_map.get(job.release_id)
                if rel is None:
                    continue
                if now - job.last_progress_ts <= rel.stage_timeout:
                    continue
                # 阶段无进展
                if job.attempts + 1 < rel.max_stage_attempts:
                    job.attempts += 1
                    job.last_progress_ts = now
                    retried.append(job.job_id)
                    self._audit("stage_retry", job.job_id,
                                f"阶段 {job.stage.value} 无进展，第 {job.attempts} 次重试",
                                device_id=job.device_id)
                else:
                    self._terminate_job(
                        job, JobStage.SUSPENDED,
                        f"watchdog_stage_timeout:{job.stage.value}", now)
                    self._after_terminal(job)
                    suspended.append(job.job_id)
            self.store.save()
        for did in offline:
            self._audit("device_offline", did, "心跳超时，标记离线")
        for jid in suspended:
            log.warning("WATCHDOG job=%s SUSPENDED", jid)
        log.info("SWEEP offline=%s retried=%s suspended=%s",
                 offline, retried, suspended)
        return {"offline": offline, "retried": retried, "suspended": suspended}

    def _resume_suspended_jobs(self, device_id: str, now: float) -> list[str]:
        """重连补齐：设备重新上线后，看门狗搁置的任务回到 DOWNLOADING 重试。"""
        resumed: list[str] = []
        for job in self.store.jobs.values():
            if job.device_id != device_id:
                continue
            if job.stage is JobStage.SUSPENDED and (
                job.terminal_reason or ""
            ).startswith("watchdog_"):
                job.stage = JobStage.DOWNLOADING
                job.stage_seq += 1
                job.attempts = 0
                job.stage_entered_ts = now
                job.last_progress_ts = now
                job.terminal_ts = None
                job.terminal_reason = None
                job.counted = False
                job.last_report_seq = -1
                resumed.append(job.job_id)
                self._audit("job_resumed", job.job_id,
                            "设备重连，看门狗搁置任务恢复", device_id=device_id)
        if resumed:
            self.store.save()
        return resumed

    # ==================================================================
    # 查询
    # ==================================================================
    def get_device(self, device_id: str) -> dict:
        with self.store.lock:
            dev = self._get_device(device_id)
            jobs = [j.to_dict() for j in self.store.jobs.values()
                    if j.device_id == device_id]
            return {"device": dev.to_dict(), "jobs": jobs}

    def get_release(self, release_id: str) -> dict:
        with self.store.lock:
            rel = self._get_release(release_id)
            stats = self._release_stats(release_id)
            return {"release": rel.to_dict(), "stats": stats,
                    "batches": [b.to_dict() for b in self._batches_of(release_id)]}

    def get_batch(self, batch_id: str) -> dict:
        with self.store.lock:
            b = self.store.batches.get(batch_id)
            if b is None:
                raise ReleaseNotFound(f"批次 {batch_id} 不存在",
                                      reason="batch_not_found")
            jobs = [j.to_dict() for j in self.store.jobs.values()
                    if j.batch_id == batch_id]
            return {"batch": b.to_dict(), "jobs": jobs}

    def audit_log(self, ref: str | None = None, limit: int = 100) -> list[dict]:
        with self.store.lock:
            events = self.store.audit
            if ref:
                events = [e for e in events if e.get("ref") == ref]
            return events[-limit:]

    # ==================================================================
    # 内部规则助手（调用方持锁）
    # ==================================================================
    def _get_device(self, device_id: str) -> Device:
        dev = self.store.devices.get(device_id)
        if dev is None:
            raise DeviceNotFound(f"设备 {device_id} 不存在", reason="unknown_device")
        return dev

    def _get_release(self, release_id: str) -> Release:
        rel = self.store.releases.get(release_id)
        if rel is None:
            raise ReleaseNotFound(f"发布 {release_id} 不存在", reason="release_missing")
        return rel

    def _batches_of(self, release_id: str) -> list[Batch]:
        return sorted(
            (b for b in self.store.batches.values() if b.release_id == release_id),
            key=lambda b: b.ordinal,
        )

    def _jobs_of(self, release_id: str) -> list[Job]:
        return [j for j in self.store.jobs.values() if j.release_id == release_id]

    def _release_of_device(self, device_id: str) -> str | None:
        for j in self.store.jobs.values():
            if j.device_id == device_id and j.stage not in TERMINAL_STAGES:
                return j.release_id
        return None

    def _eligibility_reason(self, dev: Device, target: Firmware,
                            rel: Release) -> str | None:
        """返回 None 表示可纳入；否则返回机器可读原因。"""
        if dev.model != target.model:
            return "model_mismatch"
        if dev.hw_version != target.hw_version:
            return "hw_mismatch"
        if target.yanked:
            return "firmware_yanked"
        try:
            cur = parse_version(dev.current_fw_version)
            tgt = parse_version(target.version)
        except ValueError:
            return "bad_version"
        if cur == tgt:
            return "already_on_target"
        if tgt < cur and not rel.allow_downgrade:
            return "downgrade_not_allowed"
        if rel.allowed_from_versions and dev.current_fw_version not in rel.allowed_from_versions:
            return "from_version_not_allowed"
        return None

    @staticmethod
    def _next_stage(stage: JobStage) -> JobStage:
        idx = STAGE_ORDER.index(stage)
        return STAGE_ORDER[idx + 1]

    def _advance(self, job: Job, new_stage: JobStage, now: float,
                 attempts: int) -> None:
        job.stage_seq += 1
        job.stage = new_stage
        job.attempts = attempts
        job.stage_entered_ts = now
        job.last_progress_ts = now

    def _terminate_job(self, job: Job, terminal: JobStage, reason: str,
                       now: float) -> None:
        job.stage = terminal
        job.terminal_ts = now
        job.terminal_reason = reason
        job.last_progress_ts = now
        self._after_terminal(job)

    def _on_success(self, job: Job, now: float) -> None:
        """升级 CONFIRMED 或回滚 ROLLED_BACK 成功后的收敛动作。"""
        dev = self.store.devices[job.device_id]
        target = self.store.firmwares[job.target_fw_id]
        if job.direction is JobDirection.ROLLBACK:
            job.stage = JobStage.ROLLED_BACK
        dev.current_fw_version = target.version
        job.terminal_ts = now
        job.terminal_reason = "success"

    def _after_terminal(self, job: Job) -> None:
        """终态收敛：统计 CAS、批次 drained、阈值熔断、发布完成、锁清理。"""
        if not job.counted:
            job.counted = True
        else:
            return
        batch = self.store.batches.get(job.batch_id)
        rel = self.store.releases.get(job.release_id)
        if batch is not None:
            bj = [j for j in self.store.jobs.values() if j.batch_id == batch.batch_id]
            if all(j.stage in TERMINAL_STAGES for j in bj):
                if batch.state is BatchState.ACTIVE:
                    batch.state = BatchState.DRAINED
        if rel is not None:
            self._evaluate_threshold(rel)
            self._maybe_complete(rel)
        if job.direction is JobDirection.ROLLBACK:
            self._rolling_back.discard(job.device_id)

    def _evaluate_threshold(self, rel: Release) -> None:
        """升级任务中失败/搁置占比达到阈值 → 自动停止后续批次。"""
        if rel.state not in (ReleaseState.ACTIVE, ReleaseState.PAUSED):
            return
        jobs = [j for j in self._jobs_of(rel.release_id)
                if j.direction is JobDirection.UPGRADE and j.counted]
        if not jobs:
            return
        bad = sum(1 for j in jobs
                  if j.stage in (JobStage.FAILED, JobStage.SUSPENDED))
        rate = bad / len(jobs)
        if rate + 1e-12 >= rel.failure_threshold:
            rel.state = ReleaseState.THRESHOLD_HALTED
            rel.halt_reason = (
                f"failure_rate {rate:.3f} >= threshold {rel.failure_threshold:.3f}")
            # 停止未开始的批次；活动批次中的设备继续收敛
            for b in self._batches_of(rel.release_id):
                if b.state is BatchState.ACTIVE:
                    still_active = any(
                        j.stage not in TERMINAL_STAGES
                        and j.stage is not JobStage.PENDING
                        for j in self.store.jobs.values()
                        if j.batch_id == b.batch_id
                    )
                    if not still_active:
                        b.state = BatchState.STOPPED
            now = self.clock.now()
            for j in self._jobs_of(rel.release_id):
                if j.stage is JobStage.PENDING:
                    self._terminate_job(j, JobStage.SUSPENDED,
                                        "release_threshold_halted", now)
            self._audit("release_halted", rel.release_id,
                        f"失败率 {rate:.3f} 达阈值，自动停止后续批次",
                        failure_rate=rate, threshold=rel.failure_threshold)

    def _maybe_complete(self, rel: Release) -> None:
        # PAUSED 是人工挂起：即使当前批次全部终态，剩余设备尚未纳入，
        # 绝不能自动完成；只有 ACTIVE 下全部任务终态才收敛为 COMPLETED。
        if rel.state is not ReleaseState.ACTIVE:
            return
        jobs = self._jobs_of(rel.release_id)
        if jobs and all(j.stage in TERMINAL_STAGES for j in jobs):
            rel.state = ReleaseState.COMPLETED
            rel.halt_reason = None
            self._audit("release_completed", rel.release_id,
                        "全部设备进入终态，发布完成")

    def _release_stats(self, release_id: str) -> dict[str, Any]:
        jobs = [j for j in self.store.jobs.values()
                if j.release_id == release_id and j.direction is JobDirection.UPGRADE]
        counted = [j for j in jobs if j.counted]
        confirmed = [j for j in counted if j.stage is JobStage.CONFIRMED]
        failed = [j for j in counted if j.stage is JobStage.FAILED]
        suspended = [j for j in counted if j.stage is JobStage.SUSPENDED]
        rolled = [j for j in counted if j.stage is JobStage.ROLLED_BACK]
        denom = len(counted) or 1
        return {
            "total": len(jobs),
            "confirmed": len(confirmed),
            "failed": len(failed),
            "suspended": len(suspended),
            "rolled_back": len(rolled),
            "in_flight": sum(1 for j in jobs if j.stage not in TERMINAL_STAGES),
            "failure_rate": round(len(failed) / denom, 6),
        }

    # ---- 回滚规则 ----
    def _resolve_rollback_target(self, dev: Device, target_fw_id: str,
                                 exclude_unfinished: bool) -> Firmware:
        """校验回滚是否合法；返回目标固件或抛 RollbackError 子类。"""
        # 1) 设备上不能有未终态任务（不允许对升级中的设备回滚→不留半更新）
        if exclude_unfinished:
            inflight = [
                j for j in self.store.jobs.values()
                if j.device_id == dev.device_id and j.stage not in TERMINAL_STAGES
            ]
            if inflight:
                stages = ",".join(sorted({j.stage.value for j in inflight}))
                raise RollbackNotAllowed(
                    f"设备有未终态任务({stages})，不可回滚",
                    reason="job_in_flight")
        # 2) 必须有发布历史，回滚目标必须是“曾经运行过的版本”
        history = sorted(
            (j for j in self.store.jobs.values() if j.device_id == dev.device_id),
            key=lambda j: j.created_ts,
        )
        if not history:
            raise RollbackNotAllowed("设备没有发布历史，无可回滚版本",
                                     reason="no_history")
        latest = history[-1]
        if latest.stage is JobStage.CONFIRMED and latest.direction is JobDirection.UPGRADE:
            # 只允许回到该升级任务的起点版本，保证目标真实存在且兼容
            wanted_id = target_fw_id or None
            prev_fw = self._find_firmware_by_version(
                dev.model, dev.hw_version, latest.from_version)
            if prev_fw is None:
                raise RollbackTargetMissing(
                    f"原版本固件 {latest.from_version} 已不在固件库",
                    reason="previous_fw_missing")
            if wanted_id and wanted_id != prev_fw.fw_id:
                raise RollbackNotAllowed(
                    f"只允许回滚到升级前版本 {latest.from_version}",
                    reason="target_not_previous")
            target = prev_fw
        else:
            # FAILED/SUSPENDED/已回滚 等：设备若从未离开原版本，则无版本可回
            prev_fw = self._find_firmware_by_version(
                dev.model, dev.hw_version, latest.from_version)
            if (dev.current_fw_version == latest.from_version and prev_fw is not None
                    and latest.direction is JobDirection.UPGRADE):
                raise RollbackNotAllowed(
                    f"设备仍处于升级前版本 {dev.current_fw_version}，无可回滚内容",
                    reason="nothing_to_rollback")
            # 其它情形必须显式指定存在的目标
            if not target_fw_id:
                raise RollbackTargetMissing("必须显式指定回滚目标固件",
                                            reason="target_required")
            target = self.store.firmwares.get(target_fw_id)
            if target is None:
                raise RollbackTargetMissing(
                    f"目标固件 {target_fw_id} 不存在", reason="target_fw_missing")
        # 3) 目标兼容性：型号、硬件必须匹配
        if target.model != dev.model or target.hw_version != dev.hw_version:
            raise IncompatibleFirmware("回滚目标与设备型号/硬件不匹配",
                                       reason="model_or_hw_mismatch")
        if target.yanked:
            raise RollbackNotAllowed("目标固件已被撤下", reason="firmware_yanked")
        # 4) 方向：目标不得高于当前版本（回滚只往低版本走）
        if parse_version(target.version) >= parse_version(dev.current_fw_version):
            raise RollbackNotAllowed(
                "回滚目标版本必须低于当前版本", reason="not_a_downgrade")
        return target

    def _find_firmware_by_version(self, model: str, hw_version: str,
                                  version: str) -> Firmware | None:
        for fw in self.store.firmwares.values():
            if (fw.model == model and fw.hw_version == hw_version
                    and fw.version == version and not fw.yanked):
                return fw
        return None

    def _create_rollback_job(self, dev: Device, target: Firmware, now: float,
                             reason: str, batch_id: str | None = None) -> Job:
        """建立回滚任务并把旧任务标记为被取代（可追溯链）。"""
        prior = max(
            (j for j in self.store.jobs.values() if j.device_id == dev.device_id),
            key=lambda j: j.created_ts,
        )
        job = Job(
            job_id=self.store.next_id("job"),
            release_id=prior.release_id,
            batch_id=batch_id or prior.batch_id,
            device_id=dev.device_id, target_fw_id=target.fw_id,
            from_version=dev.current_fw_version,
            direction=JobDirection.ROLLBACK, stage=JobStage.PENDING,
            stage_seq=0, attempts=0, created_ts=now,
            stage_entered_ts=now, last_progress_ts=now,
        )
        prior.superseded_by = job.job_id
        self.store.jobs[job.job_id] = job
        self._audit("rollback_job_linked", prior.job_id,
                    f"原任务被回滚任务 {job.job_id} 取代", device_id=dev.device_id,
                    target=target.version, trigger=reason)
        return job
