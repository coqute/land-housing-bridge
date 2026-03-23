"""SQLite + NumPy 벡터 저장소 — 매트릭스 캐시 기반 고속 검색.

DB 위치: data/vector.db
L2-normalized 벡터 → 코사인 유사도 = dot product.

최적화 전략:
- 검색 시 전체 임베딩을 NumPy 매트릭스로 캐시 (단일 matmul로 전체 유사도 계산)
- 쓰기 시 캐시 무효화 (dirty flag)
- WAL 모드 + PRAGMA 최적화
- 메타데이터 인덱스로 필터링 후 벡터 검색 (불필요한 임베딩 로드 방지)
"""

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB 경로
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
DB_PATH = os.path.join(_DATA_DIR, "vector.db")

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 임베딩 매트릭스 캐시 (검색 고속화)
# ---------------------------------------------------------------------------
_cache_dirty = True
_cached_matrix: np.ndarray | None = None        # (N, dim) float32
_cached_meta: list[dict] | None = None           # 매트릭스 행과 1:1 대응
_cached_filter_key: str | None = None            # 캐시된 필터 조건


# ---------------------------------------------------------------------------
# DB 초기화
# ---------------------------------------------------------------------------
def _get_conn() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            os.makedirs(_DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            # 성능 최적화 PRAGMA
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA cache_size=-64000")  # 64MB
            _conn.execute("PRAGMA mmap_size=268435456")  # 256MB
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.row_factory = sqlite3.Row
        return _conn


def init_db() -> None:
    """테이블 생성 (idempotent)."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notices (
            notice_id    TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            title        TEXT NOT NULL,
            url          TEXT,
            content_hash TEXT,
            processed_at TEXT NOT NULL,
            status       TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            notice_id   TEXT NOT NULL REFERENCES notices(notice_id) ON DELETE CASCADE,
            text        TEXT NOT NULL,
            section     TEXT NOT NULL,
            source_type TEXT NOT NULL,
            page        INTEGER,
            embedding   BLOB,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_notice ON chunks(notice_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section);
        CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedding) WHERE embedding IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_notices_source ON notices(source);
        CREATE INDEX IF NOT EXISTS idx_notices_status ON notices(status);
    """)
    conn.commit()
    logger.debug("vector_store 초기화 완료")


# ---------------------------------------------------------------------------
# 캐시 관리
# ---------------------------------------------------------------------------
def _invalidate_cache() -> None:
    """쓰기 연산 후 캐시 무효화."""
    global _cache_dirty, _cached_matrix, _cached_meta, _cached_filter_key
    _cache_dirty = True
    _cached_matrix = None
    _cached_meta = None
    _cached_filter_key = None


def _build_cache(section: str | None = None, source: str | None = None) -> tuple[np.ndarray, list[dict]]:
    """검색 조건에 맞는 임베딩 매트릭스를 빌드.

    Returns:
        (embedding_matrix: (N, dim), metadata_list: [...])
    """
    global _cache_dirty, _cached_matrix, _cached_meta, _cached_filter_key

    filter_key = f"{section}|{source}"
    if not _cache_dirty and _cached_matrix is not None and _cached_filter_key == filter_key:
        return _cached_matrix, _cached_meta

    conn = _get_conn()

    where_clauses = ["c.embedding IS NOT NULL"]
    params: list = []
    if section:
        where_clauses.append("c.section = ?")
        params.append(section)
    if source:
        where_clauses.append("n.source = ?")
        params.append(source)

    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"SELECT c.chunk_id, c.notice_id, c.text, c.section, c.source_type, c.page, "
        f"c.embedding, n.title, n.source, n.url "
        f"FROM chunks c JOIN notices n ON c.notice_id = n.notice_id "
        f"WHERE {where_sql}",
        params,
    ).fetchall()

    if not rows:
        empty_meta: list[dict] = []
        return np.empty((0, 0), dtype=np.float32), empty_meta

    # 임베딩 매트릭스 구축 (단일 NumPy 배열)
    embeddings = []
    meta = []
    for row in rows:
        emb = np.frombuffer(row["embedding"], dtype=np.float32)
        embeddings.append(emb)
        meta.append({
            "chunk_id": row["chunk_id"],
            "notice_id": row["notice_id"],
            "title": row["title"],
            "source": row["source"],
            "url": row["url"],
            "text": row["text"],
            "section": row["section"],
            "source_type": row["source_type"],
            "page": row["page"],
        })

    matrix = np.vstack(embeddings)  # (N, dim)

    # 캐시 저장
    _cached_matrix = matrix
    _cached_meta = meta
    _cached_filter_key = filter_key
    _cache_dirty = False

    logger.debug(f"벡터 캐시 빌드: {matrix.shape[0]}건, dim={matrix.shape[1]}")
    return matrix, meta


# ---------------------------------------------------------------------------
# 해시·시간 유틸
# ---------------------------------------------------------------------------
def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Notice CRUD
# ---------------------------------------------------------------------------
def upsert_notice(
    notice_id: str,
    source: str,
    title: str,
    url: str = "",
    content_hash: str = "",
) -> bool:
    """공고 메타 upsert. content_hash 변경 시 True (재임베딩 필요)."""
    init_db()
    conn = _get_conn()
    row = conn.execute(
        "SELECT content_hash FROM notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()

    now = _now_iso()
    if row is None:
        conn.execute(
            "INSERT INTO notices (notice_id, source, title, url, content_hash, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (notice_id, source, title, url, content_hash, now),
        )
        conn.commit()
        _invalidate_cache()
        return True

    if row["content_hash"] == content_hash:
        return False

    conn.execute(
        "UPDATE notices SET source=?, title=?, url=?, content_hash=?, processed_at=?, status='active' "
        "WHERE notice_id=?",
        (source, title, url, content_hash, now, notice_id),
    )
    conn.commit()
    _invalidate_cache()
    return True


def close_notice(notice_id: str) -> None:
    conn = _get_conn()
    conn.execute("UPDATE notices SET status='closed' WHERE notice_id=?", (notice_id,))
    conn.commit()


def delete_notice(notice_id: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM notices WHERE notice_id=?", (notice_id,))
    conn.commit()
    _invalidate_cache()


def get_all_notice_ids(source: str | None = None) -> set[str]:
    init_db()
    conn = _get_conn()
    if source:
        rows = conn.execute(
            "SELECT notice_id FROM notices WHERE source=?", (source,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT notice_id FROM notices").fetchall()
    return {r["notice_id"] for r in rows}


def get_notice_info(notice_id: str) -> dict | None:
    """공고 메타 정보 조회."""
    init_db()
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM notices WHERE notice_id=?", (notice_id,)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Chunk CRUD
# ---------------------------------------------------------------------------
def _embed_to_blob(embedding: list[float]) -> bytes:
    return np.array(embedding, dtype=np.float32).tobytes()


def store_chunks(
    notice_id: str,
    chunks: list[dict],
    embeddings: list[list[float]] | None = None,
) -> int:
    """공고 청크 교체 저장 (atomic)."""
    init_db()
    conn = _get_conn()
    now = _now_iso()

    conn.execute("DELETE FROM chunks WHERE notice_id=?", (notice_id,))

    stored = 0
    for i, chunk in enumerate(chunks):
        emb_blob = None
        if embeddings and i < len(embeddings):
            emb_blob = _embed_to_blob(embeddings[i])

        conn.execute(
            "INSERT INTO chunks (notice_id, text, section, source_type, page, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                notice_id,
                chunk.get("text", ""),
                chunk.get("section", "body"),
                chunk.get("source_type", "title"),
                chunk.get("page"),
                emb_blob,
                now,
            ),
        )
        stored += 1

    conn.commit()
    _invalidate_cache()
    return stored


def get_notice_chunks(notice_id: str) -> list[dict]:
    init_db()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT chunk_id, text, section, source_type, page, created_at "
        "FROM chunks WHERE notice_id=? ORDER BY chunk_id",
        (notice_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 벡터 검색 (매트릭스 캐시 기반)
# ---------------------------------------------------------------------------
def search(
    query_embedding: list[float],
    top_k: int = 10,
    section: str | None = None,
    source: str | None = None,
    min_score: float = 0.0,
    exclude_notice_ids: set[str] | None = None,
) -> list[dict]:
    """코사인 유사도 기반 청크 검색 (매트릭스 matmul 최적화).

    L2-normalized 벡터이므로 dot product = cosine similarity.
    전체 임베딩을 (N, dim) 매트릭스로 캐시하고 단일 matmul로 유사도 일괄 계산.
    """
    init_db()
    matrix, meta = _build_cache(section, source)

    if matrix.size == 0:
        return []

    query_vec = np.array(query_embedding, dtype=np.float32)

    # 단일 matmul: (N, dim) @ (dim,) → (N,) 유사도 벡터
    scores = matrix @ query_vec

    # 필터링 + 정렬
    exclude = exclude_notice_ids or set()
    indices = np.argsort(scores)[::-1]  # 내림차순

    results = []
    for idx in indices:
        if len(results) >= top_k:
            break
        score = float(scores[idx])
        if score < min_score:
            break
        m = meta[idx]
        if m["notice_id"] in exclude:
            continue
        results.append({**m, "score": score})

    return results


def search_by_notice(
    query_embedding: list[float],
    top_k: int = 5,
    source: str | None = None,
    exclude_notice_ids: set[str] | None = None,
) -> list[dict]:
    """공고 단위 유사도 검색 (청크 점수 평균으로 랭킹)."""
    chunk_results = search(
        query_embedding, top_k=top_k * 10, source=source,
        exclude_notice_ids=exclude_notice_ids,
    )

    groups: dict[str, dict] = {}
    for r in chunk_results:
        nid = r["notice_id"]
        if nid not in groups:
            groups[nid] = {
                "notice_id": nid,
                "title": r["title"],
                "source": r["source"],
                "url": r["url"],
                "scores": [],
                "top_chunks": [],
            }
        groups[nid]["scores"].append(r["score"])
        if len(groups[nid]["top_chunks"]) < 3:
            groups[nid]["top_chunks"].append(r)

    notice_results = []
    for g in groups.values():
        g["avg_score"] = sum(g["scores"]) / len(g["scores"])
        del g["scores"]
        notice_results.append(g)

    notice_results.sort(key=lambda x: x["avg_score"], reverse=True)
    return notice_results[:top_k]


def get_notice_embedding(notice_id: str) -> list[float] | None:
    """공고의 평균 임베딩 벡터 반환 (유사 공고 검색용)."""
    init_db()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT embedding FROM chunks WHERE notice_id=? AND embedding IS NOT NULL",
        (notice_id,),
    ).fetchall()

    if not rows:
        return None

    embeddings = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
    avg_vec = np.mean(embeddings, axis=0)
    # L2 정규화 (평균 벡터는 정규화 필요)
    norm = np.linalg.norm(avg_vec)
    if norm > 0:
        avg_vec = avg_vec / norm
    return avg_vec.tolist()


# ---------------------------------------------------------------------------
# 통계
# ---------------------------------------------------------------------------
def get_stats() -> dict:
    init_db()
    conn = _get_conn()

    total_notices = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    active_notices = conn.execute(
        "SELECT COUNT(*) FROM notices WHERE status='active'"
    ).fetchone()[0]
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embedded_chunks = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
    ).fetchone()[0]

    by_source = {}
    for row in conn.execute(
        "SELECT n.source, COUNT(*) FROM chunks c JOIN notices n ON c.notice_id=n.notice_id GROUP BY n.source"
    ).fetchall():
        by_source[row[0]] = row[1]

    by_section = {}
    for row in conn.execute(
        "SELECT section, COUNT(*) FROM chunks GROUP BY section"
    ).fetchall():
        by_section[row[0]] = row[1]

    # 임베딩 차원 정보
    dim = 0
    sample = conn.execute(
        "SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if sample and sample["embedding"]:
        dim = len(np.frombuffer(sample["embedding"], dtype=np.float32))

    return {
        "total_notices": total_notices,
        "active_notices": active_notices,
        "total_chunks": total_chunks,
        "embedded_chunks": embedded_chunks,
        "embedding_dim": dim,
        "by_source": by_source,
        "by_section": by_section,
    }


def needs_update(notice_id: str, content_text: str) -> bool:
    """content_hash 비교로 재처리 필요 여부 판단."""
    init_db()
    conn = _get_conn()
    row = conn.execute(
        "SELECT content_hash FROM notices WHERE notice_id=?", (notice_id,)
    ).fetchone()
    if row is None:
        return True
    return row["content_hash"] != _text_hash(content_text)


def content_hash(text: str) -> str:
    return _text_hash(text)
