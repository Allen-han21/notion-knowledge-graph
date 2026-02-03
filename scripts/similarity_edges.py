#!/usr/bin/env python3
"""
벡터 유사도 기반 SIMILAR_TO 관계 생성
Qdrant → Neo4j
"""

import os
from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from tqdm import tqdm
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# Neo4j 설정
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")

if not NEO4J_PASSWORD:
    raise RuntimeError("NEO4J_PASSWORD 환경 변수가 설정되지 않았습니다.")

# Qdrant 설정
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "notion_pages")

# 유사도 임계값
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", 0.75))  # 75% 이상
TOP_K = 5  # 각 페이지당 상위 5개만


def create_similarity_edges():
    """벡터 유사도 기반 관계 생성"""
    print("Creating SIMILAR_TO edges from vector similarity...")
    print(f"Threshold: {SIMILARITY_THRESHOLD} ({SIMILARITY_THRESHOLD*100:.0f}%)")

    # 클라이언트 초기화
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # Qdrant에서 모든 포인트 가져오기
    print("Fetching vectors from Qdrant...")
    all_points = []
    offset = None

    while True:
        result = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            offset=offset,
            with_vectors=True,
            with_payload=True
        )
        points, offset = result
        all_points.extend(points)

        if offset is None:
            break

    print(f"Fetched {len(all_points)} points")

    # 유사도 관계 생성
    stats = {"created": 0, "skipped": 0}

    with neo4j_driver.session() as session:
        # 기존 SIMILAR_TO 관계 삭제
        session.run("MATCH ()-[r:SIMILAR_TO]->() DELETE r")
        print("Cleared existing SIMILAR_TO relationships")

        for point in tqdm(all_points, desc="Creating SIMILAR_TO"):
            notion_id = point.payload.get("notion_id", "")
            if not notion_id:
                continue

            # 유사한 페이지 검색
            results = qdrant.query_points(
                collection_name=COLLECTION_NAME,
                query=point.vector,
                limit=TOP_K + 1,  # 자기 자신 제외
                with_payload=True
            )

            for hit in results.points:
                # 자기 자신 제외
                similar_id = hit.payload.get("notion_id", "")
                if similar_id == notion_id:
                    continue

                # 임계값 이상만
                if hit.score < SIMILARITY_THRESHOLD:
                    stats["skipped"] += 1
                    continue

                # Neo4j에 관계 생성
                try:
                    result = session.run("""
                        MATCH (p1:Page {id: $id1})
                        MATCH (p2:Page {id: $id2})
                        MERGE (p1)-[r:SIMILAR_TO]->(p2)
                        SET r.score = $score
                        RETURN count(r) as created
                    """, id1=notion_id, id2=similar_id, score=round(hit.score, 3))

                    if result.single()["created"] > 0:
                        stats["created"] += 1
                except Exception:
                    pass

    neo4j_driver.close()

    print(f"\n✅ Created {stats['created']} SIMILAR_TO relationships")
    print(f"Skipped {stats['skipped']} (below threshold {SIMILARITY_THRESHOLD})")


if __name__ == "__main__":
    create_similarity_edges()
