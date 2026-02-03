#!/usr/bin/env python3
"""
Kidsnote iOS 소스코드 벡터 임베딩
Swift 파일 → BGE-M3 (1024D) → Qdrant

Phase 5: 코드베이스 임베딩
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
import hashlib

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, PayloadSchemaType
from FlagEmbedding import BGEM3FlagModel
from tqdm import tqdm

# 경로 설정
KIDSNOTE_IOS_PATH = Path.home() / "Dev" / "Repo" / "kidsnote_ios" / "Sources"
DATA_DIR = Path.home() / ".claude" / "notion-graph" / "data"

# Qdrant 설정
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "kidsnote_ios"
VECTOR_DIM = 1024  # BGE-M3 dense 벡터 차원

# 배치 설정
BATCH_SIZE = 4  # CPU에서 안정적인 크기
MAX_TEXT_LENGTH = 4096  # 코드는 긴 파일이 많으므로 늘림
MAX_CHARS = 8000  # BGE-M3 토큰 제한 고려
UPSERT_BATCH_SIZE = 20


def find_swift_files(source_dir: Path) -> list[Path]:
    """Sources 디렉토리에서 Swift 파일 찾기"""
    print(f"Scanning {source_dir} for Swift files...")
    swift_files = list(source_dir.rglob("*.swift"))
    print(f"Found {len(swift_files)} Swift files")
    return swift_files


def extract_metadata(file_path: Path, base_path: Path) -> dict:
    """파일 경로에서 메타데이터 추출"""
    relative = file_path.relative_to(base_path)
    parts = relative.parts

    # 모듈 추출 (첫 번째 폴더)
    module = parts[0] if len(parts) > 1 else "Root"

    # 하위 폴더 경로
    subpath = "/".join(parts[1:-1]) if len(parts) > 2 else ""

    return {
        "file_name": file_path.name,
        "module": module,
        "subpath": subpath,
        "relative_path": str(relative),
        "extension": file_path.suffix,
    }


def extract_swift_info(content: str) -> dict:
    """Swift 코드에서 정보 추출"""
    info = {
        "imports": [],
        "classes": [],
        "structs": [],
        "enums": [],
        "protocols": [],
        "extensions": [],
        "functions": [],
    }

    # import 문
    imports = re.findall(r'^import\s+(\w+)', content, re.MULTILINE)
    info["imports"] = list(set(imports))

    # class 정의
    classes = re.findall(r'(?:final\s+)?class\s+(\w+)', content)
    info["classes"] = list(set(classes))

    # struct 정의
    structs = re.findall(r'struct\s+(\w+)', content)
    info["structs"] = list(set(structs))

    # enum 정의
    enums = re.findall(r'enum\s+(\w+)', content)
    info["enums"] = list(set(enums))

    # protocol 정의
    protocols = re.findall(r'protocol\s+(\w+)', content)
    info["protocols"] = list(set(protocols))

    # extension
    extensions = re.findall(r'extension\s+(\w+)', content)
    info["extensions"] = list(set(extensions))

    # 함수 (public/internal/private func)
    functions = re.findall(r'(?:public|internal|private|open|fileprivate)?\s*func\s+(\w+)', content)
    info["functions"] = list(set(functions))[:20]  # 상위 20개만

    return info


def prepare_code_for_embedding(content: str, metadata: dict) -> str:
    """임베딩용 텍스트 준비 (파일명 + 주요 정보 + 코드)"""
    file_name = metadata.get("file_name", "")
    module = metadata.get("module", "")

    # 헤더 정보
    header = f"File: {file_name}\nModule: {module}\n\n"

    # 코드 내용 (주석 포함, 의미 파악에 도움)
    text = header + content

    # 길이 제한
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    return text


def file_to_uuid(file_path: str) -> str:
    """파일 경로를 UUID로 변환 (일관된 ID 생성)"""
    hash_obj = hashlib.md5(file_path.encode())
    hex_digest = hash_obj.hexdigest()
    # UUID 형식으로 변환
    return f"{hex_digest[:8]}-{hex_digest[8:12]}-{hex_digest[12:16]}-{hex_digest[16:20]}-{hex_digest[20:32]}"


def init_model() -> BGEM3FlagModel:
    """BGE-M3 모델 초기화"""
    print("Loading BGE-M3 model...")
    model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)
    print("Model loaded successfully")
    return model


def init_qdrant(recreate: bool = True) -> QdrantClient:
    """Qdrant 클라이언트 초기화 및 컬렉션 생성"""
    print(f"Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}...")
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    collections = [c.name for c in client.get_collections().collections]

    if COLLECTION_NAME in collections:
        if recreate:
            print(f"Collection '{COLLECTION_NAME}' already exists. Recreating...")
            client.delete_collection(COLLECTION_NAME)
        else:
            print(f"Collection '{COLLECTION_NAME}' already exists. Using existing.")
            return client

    # 컬렉션 생성
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=VECTOR_DIM,
            distance=Distance.COSINE
        )
    )

    # 페이로드 인덱스 생성
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="module",
        field_schema=PayloadSchemaType.KEYWORD
    )
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="lines",
        field_schema=PayloadSchemaType.INTEGER
    )

    print(f"Collection '{COLLECTION_NAME}' created with {VECTOR_DIM}D vectors")
    return client


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


def process_files(
    files: list[Path],
    base_path: Path,
    model: BGEM3FlagModel,
    client: QdrantClient
) -> dict:
    """파일 임베딩 및 Qdrant 저장"""

    stats = {
        "total": len(files),
        "processed": 0,
        "skipped_empty": 0,
        "errors": 0,
        "modules": set()
    }

    # 파일 읽기 및 준비
    valid_files = []
    for file_path in tqdm(files, desc="Reading files"):
        try:
            content = file_path.read_text(encoding='utf-8')
            if not content.strip():
                stats["skipped_empty"] += 1
                continue

            metadata = extract_metadata(file_path, base_path)
            swift_info = extract_swift_info(content)
            metadata.update(swift_info)

            text = prepare_code_for_embedding(content, metadata)
            valid_files.append((file_path, content, metadata, text))
            stats["modules"].add(metadata["module"])

        except Exception as e:
            stats["errors"] += 1
            continue

    print(f"\nPrepared {len(valid_files)} files for embedding")
    print(f"Modules: {sorted(stats['modules'])}")

    # 배치 임베딩
    points = []

    for i in tqdm(range(0, len(valid_files), BATCH_SIZE), desc="Embedding"):
        batch = valid_files[i:i + BATCH_SIZE]
        batch_texts = [item[3] for item in batch]

        try:
            vectors = embed_batch(model, batch_texts)

            for (file_path, content, metadata, _), vector in zip(batch, vectors):
                lines = len(content.splitlines())

                point = PointStruct(
                    id=file_to_uuid(str(file_path.relative_to(base_path))),
                    vector=vector,
                    payload={
                        "file_name": metadata["file_name"],
                        "module": metadata["module"],
                        "subpath": metadata["subpath"],
                        "relative_path": metadata["relative_path"],
                        "lines": lines,
                        "imports": metadata.get("imports", []),
                        "classes": metadata.get("classes", []),
                        "structs": metadata.get("structs", []),
                        "protocols": metadata.get("protocols", []),
                        "content_preview": content[:1000],
                    }
                )
                points.append(point)
                stats["processed"] += 1

        except Exception as e:
            print(f"\nError processing batch: {e}")
            stats["errors"] += BATCH_SIZE
            continue

        # 주기적으로 업서트
        if len(points) >= UPSERT_BATCH_SIZE:
            try:
                client.upsert(collection_name=COLLECTION_NAME, points=points)
                points = []
            except Exception as e:
                print(f"\nQdrant upsert error: {e}")
                stats["errors"] += len(points)
                points = []

    # 남은 포인트 업서트
    if points:
        client.upsert(collection_name=COLLECTION_NAME, points=points)

    stats["modules"] = sorted(stats["modules"])
    return stats


def test_code_search(client: QdrantClient, model: BGEM3FlagModel):
    """코드 검색 테스트"""
    print("\n" + "="*60)
    print("Code Search Test")
    print("="*60)

    test_queries = [
        "로그인 인증 처리",
        "네트워크 API 호출",
        "테이블뷰 셀 구현",
        "푸시 알림 처리",
        "ReactorKit 사용",
    ]

    for query in test_queries:
        print(f"\n🔍 Query: \"{query}\"")
        print("-" * 40)

        query_vector = model.encode(
            [query],
            max_length=512,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False
        )['dense_vecs'][0].tolist()

        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=5,
            with_payload=True
        )

        for i, hit in enumerate(results.points, 1):
            file_name = hit.payload.get("file_name", "Unknown")
            module = hit.payload.get("module", "")
            score = hit.score
            classes = hit.payload.get("classes", [])
            print(f"  {i}. [{score:.3f}] {module}/{file_name}")
            if classes:
                print(f"     Classes: {', '.join(classes[:3])}")


def save_metadata(stats: dict, output_path: Path):
    """메타데이터 저장"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print(f"Metadata saved to {output_path}")


def main():
    start_time = datetime.now()
    print(f"Starting code embedding at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    print(f"Source: {KIDSNOTE_IOS_PATH}")
    print(f"Collection: {COLLECTION_NAME}")
    print("="*60)

    # 1. Swift 파일 찾기
    swift_files = find_swift_files(KIDSNOTE_IOS_PATH)

    if not swift_files:
        print("No Swift files found!")
        return

    # 2. 모델 초기화
    model = init_model()

    # 3. Qdrant 초기화
    client = init_qdrant(recreate=True)

    # 4. 임베딩 및 저장
    stats = process_files(swift_files, KIDSNOTE_IOS_PATH, model, client)

    # 5. 결과 출력
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    print("\n" + "="*60)
    print("Code Embedding Complete!")
    print("="*60)
    print(f"Total files: {stats['total']}")
    print(f"Processed: {stats['processed']}")
    print(f"Skipped (empty): {stats['skipped_empty']}")
    print(f"Errors: {stats['errors']}")
    print(f"Modules: {len(stats['modules'])}")
    print(f"Duration: {duration:.1f} seconds ({duration/60:.1f} minutes)")

    # 6. 컬렉션 정보 확인
    collection_info = client.get_collection(COLLECTION_NAME)
    print(f"\nQdrant Collection Info:")
    print(f"  Points count: {collection_info.points_count}")
    print(f"  Vector size: {collection_info.config.params.vectors.size}")

    # 7. 메타데이터 저장
    save_metadata({
        "timestamp": start_time.isoformat(),
        "total_files": stats["total"],
        "processed": stats["processed"],
        "modules": stats["modules"],
        "duration_seconds": duration,
        "collection_name": COLLECTION_NAME,
        "vector_dim": VECTOR_DIM
    }, DATA_DIR / "code_embedding_stats.json")

    # 8. 검색 테스트
    test_code_search(client, model)

    print("\n✅ Phase 5.1 Complete! (Code Embedding)")


if __name__ == "__main__":
    main()
