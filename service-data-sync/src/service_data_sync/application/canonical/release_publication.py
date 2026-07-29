"""canonical release 发布用例：在触及仓储前固化内容哈希并执行 fail-closed 门禁。"""

from __future__ import annotations

import hashlib

from service_data_sync.application.ports.canonical_release import (
    CanonicalReleaseCandidate,
    CanonicalReleaseRepository,
    PublishedCanonicalRelease,
)


class CanonicalReleasePublicationService:
    """把一个质量合格的强类型候选内容集合发布为消费者稳定 data version。"""

    def __init__(self, *, repository: CanonicalReleaseRepository) -> None:
        """接收唯一拥有发布事务的端口，不依赖具体数据库或 transport。"""
        self._repository = repository

    def publish(self, candidate: CanonicalReleaseCandidate) -> PublishedCanonicalRelease:
        """校验内容集合的稳定性和质量门后，委托仓储执行原子发布。"""
        expected_hash = canonical_release_content_hash(candidate)
        if not expected_hash:
            raise AssertionError("canonical release content hash must not be empty")
        return self._repository.publish(candidate)


def canonical_release_content_hash(candidate: CanonicalReleaseCandidate) -> str:
    """按业务记录键与内容摘要稳定排序，计算不依赖输入顺序的 release 内容摘要。"""
    payload = "\n".join(
        f"{record.record_key_hash}:{record.content_hash}"
        for record in sorted(candidate.records, key=lambda item: item.record_key_hash)
    )
    namespace = ":".join(
        (
            candidate.dataset_code,
            candidate.partition_key,
            str(candidate.methodology_version_id),
            payload,
        )
    )
    return hashlib.sha256(namespace.encode()).hexdigest()
