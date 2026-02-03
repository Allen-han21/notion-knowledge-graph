#!/usr/bin/env python3
"""
Notion 페이지 벡터 임베딩 및 Qdrant 저장
BGE-M3 (1024차원 dense vector) + Qdrant
"""

import json
import os
import uuid
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, PayloadSchemaType
from FlagEmbedding import BGEM3FlagModel
from tqdm import tqdm

# 환경 변수 로드
load_dotenv()

# 경로 설정
DATA_DIR = Path(__file__).parent.parent / "data"
PAGES_FILE = DATA_DIR / "pages.json"

# Qdrant 설정
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "notion_pages")
VECTOR_DIM = int(os.environ.get("VECTOR_DIM", 1024))  # BGE-M3 dense 벡터 차원

# 배치 설정
BATCH_SIZE = 4  # CPU에서 안정적인 크기
MAX_TEXT_LENGTH = 2048  # 속도를 위해 제한 (원본 8192)
UPSERT_BATCH_SIZE = 10  # Qdrant 업서트 빈도


def load_pages() -> list[dict]:
    """pages.json 로드"""
    print(f"Loading pages from {PAGES_FILE}...")
    with open(PAGES_FILE, "r", encoding="utf-8") as f:
        pages = json.load(f)
    print(f"Loaded {len(pages)} pages")
    return pages


def init_model() -> BGEM3FlagModel:
    """BGE-M3 모델 초기화"""
    print("Loading BGE-M3 model...")
    model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)
    print("Model loaded successfully")
    return model


def init_qdrant() -> QdrantClient:
    """Qdrant 클라이언트 초기화 및 컬렉션 생성"""
    print(f"Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}...")
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # 기존 컬렉션 확인
    collections = [c.name for c in client.get_collections().collections]

    if COLLECTION_NAME in collections:
        print(f"Collection '{COLLECTION_NAME}' already exists. Recreating...")
        client.delete_collection(COLLECTION_NAME)

    # 컬렉션 생성
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=VECTOR_DIM,
            distance=Distance.COSINE
        )
    )

    # 페이로드 인덱스 생성 (필터링 성능 향상)
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="word_count",
        field_schema=PayloadSchemaType.INTEGER
    )

    print(f"Collection '{COLLECTION_NAME}' created with {VECTOR_DIM}D vectors")
    return client


def prepare_text_for_embedding(page: dict) -> str:
    """임베딩용 텍스트 준비 (제목 + 내용)"""
    title = page.get("title", "")
    content = page.get("content", "")

    # 제목을 앞에 붙여서 가중치 부여
    text = f"{title}\n\n{content}" if content else title

    # 길이 제한 (대략적인 토큰 추정, 한글은 ~2 chars per token)
    max_chars = MAX_TEXT_LENGTH * 2
    if len(text) > max_chars:
        text = text[:max_chars]

    return text


def embed_batch(model: BGEM3FlagModel, texts: list[str]) -> list[list[float]]:
    """배치 임베딩"""
    result = model.encode(
        texts,
        batch_size=len(texts),
        max_length=MAX_TEXT_LENGTH,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False
    )
    return result['dense_vecs'].tolist()


def notion_id_to_uuid(notion_id: str) -> str:
    """Notion ID를 Qdrant용 UUID로 변환"""
    clean_id = notion_id.replace("-", "")
    return str(uuid.UUID(clean_id))


def process_pages(
    pages: list[dict],
    model: BGEM3FlagModel,
    client: QdrantClient
) -> dict:
    """페이지 임베딩 및 Qdrant 저장"""

    stats = {
        "total": len(pages),
        "processed": 0,
        "skipped_empty": 0,
        "errors": 0
    }

    # 콘텐츠가 있는 페이지만 필터링
    valid_pages = []
    for page in pages:
        text = prepare_text_for_embedding(page)
        if text.strip():
            valid_pages.append((page, text))
        else:
            stats["skipped_empty"] += 1

    print(f"\nProcessing {len(valid_pages)} pages with content...")
    print(f"Skipped {stats['skipped_empty']} empty pages")

    # 배치 처리
    points = []

    for i in tqdm(range(0, len(valid_pages), BATCH_SIZE), desc="Embedding"):
        batch = valid_pages[i:i + BATCH_SIZE]
        batch_pages = [p[0] for p in batch]
        batch_texts = [p[1] for p in batch]

        try:
            # 임베딩 생성
            vectors = embed_batch(model, batch_texts)

            # 포인트 생성
            for page, vector in zip(batch_pages, vectors):
                point = PointStruct(
                    id=notion_id_to_uuid(page["id"]),
                    vector=vector,
                    payload={
                        "notion_id": page["id"],
                        "title": page.get("title", ""),
                        "created_time": page.get("created_time", ""),
                        "last_edited_time": page.get("last_edited_time", ""),
                        "url": page.get("url", ""),
                        "word_count": page.get("word_count", 0),
                        "block_count": page.get("block_count", 0),
                        "parent_id": page.get("parent", {}).get("page_id", ""),
                        "content_preview": page.get("content", "")[:500],
                        "tags": page.get("tags", [])
                    }
                )
                points.append(point)
                stats["processed"] += 1

        except Exception as e:
            print(f"\nError processing batch: {e}")
            stats["errors"] += BATCH_SIZE
            continue

        # UPSERT_BATCH_SIZE개마다 Qdrant에 업서트
        if len(points) >= UPSERT_BATCH_SIZE:
            try:
                client.upsert(collection_name=COLLECTION_NAME, points=points)
                points = []
            except Exception as e:
                print(f"\nQdrant upsert error: {e}")
                import time
                time.sleep(2)
                try:
                    client.upsert(collection_name=COLLECTION_NAME, points=points)
                    points = []
                except:
                    stats["errors"] += len(points)
                    points = []

    # 남은 포인트 업서트
    if points:
        client.upsert(collection_name=COLLECTION_NAME, points=points)

    return stats


def test_semantic_search(client: QdrantClient, model: BGEM3FlagModel):
    """의미 검색 테스트"""
    print("\n" + "="*60)
    print("Semantic Search Test")
    print("="*60)

    test_queries = [
        "프로젝트 관리 방법",
        "아키텍처 설계 패턴",
        "개인 목표 설정",
    ]

    for query in test_queries:
        print(f"\n🔍 Query: \"{query}\"")
        print("-" * 40)

        # 쿼리 임베딩
        query_vector = model.encode(
            [query],
            max_length=512,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False
        )['dense_vecs'][0].tolist()

        # 검색
        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=3,
            with_payload=True
        )

        for i, hit in enumerate(results.points, 1):
            title = hit.payload.get("title", "Untitled")
            score = hit.score
            preview = hit.payload.get("content_preview", "")[:100]
            print(f"  {i}. [{score:.3f}] {title}")
            print(f"     {preview}...")


def main():
    start_time = datetime.now()
    print(f"Starting vector embedding at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    # 1. 데이터 로드
    pages = load_pages()

    # 2. 모델 초기화
    model = init_model()

    # 3. Qdrant 초기화
    client = init_qdrant()

    # 4. 임베딩 및 저장
    stats = process_pages(pages, model, client)

    # 5. 결과 출력
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    print("\n" + "="*60)
    print("Embedding Complete!")
    print("="*60)
    print(f"Total pages: {stats['total']}")
    print(f"Processed: {stats['processed']}")
    print(f"Skipped (empty): {stats['skipped_empty']}")
    print(f"Errors: {stats['errors']}")
    print(f"Duration: {duration:.1f} seconds ({duration/60:.1f} minutes)")

    # 6. 컬렉션 정보 확인
    collection_info = client.get_collection(COLLECTION_NAME)
    print(f"\nQdrant Collection Info:")
    print(f"  Points count: {collection_info.points_count}")
    print(f"  Vector size: {collection_info.config.params.vectors.size}")

    # 7. 의미 검색 테스트
    test_semantic_search(client, model)

    print("\n✅ Vector embedding complete!")


if __name__ == "__main__":
    main()
