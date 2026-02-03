#!/usr/bin/env python3
"""
지식 그래프 인사이트 탐색
"""

import os
from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, Range
from FlagEmbedding import BGEM3FlagModel
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


def run_query(session, query, title):
    """Cypher 쿼리 실행 및 결과 출력"""
    print(f"\n{'='*60}")
    print(f"📊 {title}")
    print("="*60)

    result = session.run(query)
    records = list(result)

    if not records:
        print("  (결과 없음)")
        return

    for i, record in enumerate(records, 1):
        values = [f"{k}: {v}" for k, v in record.items()]
        print(f"  {i}. {' | '.join(values)}")


def explore_graph_insights():
    """그래프 인사이트 탐색"""
    print("\n" + "🔮 " * 20)
    print("Notion 지식 그래프 인사이트 탐색")
    print("🔮 " * 20)

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    with driver.session() as session:
        # 1. 그래프 통계
        run_query(session, """
            MATCH (n)
            RETURN labels(n)[0] as NodeType, count(*) as Count
            UNION ALL
            MATCH ()-[r]->()
            RETURN type(r) as NodeType, count(*) as Count
        """, "그래프 통계")

        # 2. 허브 노드
        run_query(session, """
            MATCH (p:Page)
            OPTIONAL MATCH (p)-[r:SIMILAR_TO|LINKS_TO|CHILD_OF]-()
            WITH p, count(r) as connections
            WHERE connections > 0
            ORDER BY connections DESC
            LIMIT 10
            RETURN p.title as Title, connections as Connections
        """, "허브 노드 (가장 많이 연결된 페이지)")

        # 3. 강한 유사도 클러스터
        run_query(session, """
            MATCH (p1:Page)-[r:SIMILAR_TO]->(p2:Page)
            WHERE r.score > 0.85
            RETURN p1.title as Page1, p2.title as Page2, r.score as Similarity
            ORDER BY r.score DESC
            LIMIT 10
        """, "강한 유사도 클러스터 (85% 이상)")

        # 4. 고립된 페이지
        run_query(session, """
            MATCH (p:Page)
            WHERE NOT (p)-[:SIMILAR_TO|LINKS_TO|CHILD_OF]-()
            AND p.wordCount > 50
            RETURN p.title as Title, p.wordCount as Words
            ORDER BY p.wordCount DESC
            LIMIT 10
        """, "고립된 페이지 (50단어 이상, 연결 없음)")

        # 5. 월별 생성 패턴
        run_query(session, """
            MATCH (p:Page)-[:CREATED_ON]->(d:Date)
            WHERE d.year >= 2024
            RETURN d.year as Year, d.month as Month, count(p) as Pages
            ORDER BY d.year, d.month
        """, "월별 페이지 생성 패턴 (2024년~)")

        # 6. 최근 12개월 추이
        run_query(session, """
            MATCH (p:Page)
            WHERE p.createdAt IS NOT NULL AND p.createdAt <> ''
            WITH p,
                 toInteger(substring(p.createdAt, 5, 2)) as month,
                 toInteger(substring(p.createdAt, 8, 2)) as day,
                 toInteger(substring(p.createdAt, 0, 4)) as year
            RETURN year as Year, month as Month, count(p) as Pages
            ORDER BY year DESC, month DESC
            LIMIT 12
        """, "최근 12개월 페이지 생성 추이")

        # 7. 가장 깊은 계층
        run_query(session, """
            MATCH path = (leaf:Page)-[:CHILD_OF*]->(root:Page)
            WHERE NOT ()-[:CHILD_OF]->(leaf)
            WITH leaf, root, length(path) as depth
            ORDER BY depth DESC
            LIMIT 5
            RETURN leaf.title as LeafPage, root.title as RootPage, depth as Depth
        """, "가장 깊은 페이지 계층")

        # 8. 브릿지 노드
        run_query(session, """
            MATCH (p:Page)-[:SIMILAR_TO]-(neighbor)
            WITH p, count(DISTINCT neighbor) as neighborCount
            WHERE neighborCount >= 5
            RETURN p.title as Title, neighborCount as SimilarPages, p.wordCount as Words
            ORDER BY neighborCount DESC
            LIMIT 10
        """, "브릿지 노드 (5개 이상 유사 페이지 연결)")

        # 9. 링크 관계
        run_query(session, """
            MATCH (p1:Page)-[:LINKS_TO]->(p2:Page)
            RETURN p1.title as From, p2.title as To
            LIMIT 15
        """, "페이지 링크 관계 (상위 15개)")

        # 10. 콘텐츠 풍부 페이지
        run_query(session, """
            MATCH (p:Page)
            WHERE p.wordCount > 500
            RETURN p.title as Title, p.wordCount as Words, p.blockCount as Blocks
            ORDER BY p.wordCount DESC
            LIMIT 10
        """, "콘텐츠가 풍부한 페이지 (500단어 이상)")

    driver.close()


def hybrid_search_demo():
    """하이브리드 검색 (벡터 + 그래프) 데모"""
    print("\n" + "🔍 " * 20)
    print("하이브리드 검색 데모 (벡터 + 그래프 확장)")
    print("🔍 " * 20)

    # 모델 및 클라이언트 초기화
    print("\nLoading BGE-M3 model...")
    model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    queries = [
        "프로젝트 관리와 일정 계획",
        "아키텍처 설계 패턴",
        "개인 목표와 성장",
    ]

    # 50단어 이상 필터
    word_filter = Filter(
        must=[FieldCondition(key='word_count', range=Range(gte=50))]
    )

    for query in queries:
        print(f"\n{'='*60}")
        print(f"🔍 Query: \"{query}\"")
        print("="*60)

        # 1. 벡터 검색
        print("\n📌 벡터 검색 결과:")
        vec = model.encode(
            [query],
            max_length=512,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False
        )['dense_vecs'][0].tolist()

        results = qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=vec,
            limit=3,
            with_payload=True,
            query_filter=word_filter
        )

        seed_ids = []
        for i, hit in enumerate(results.points, 1):
            title = hit.payload.get('title', 'Untitled')
            notion_id = hit.payload.get('notion_id', '')
            score = hit.score
            print(f"  {i}. [{score:.3f}] {title}")
            seed_ids.append(notion_id)

        # 2. 그래프 확장
        print("\n📌 그래프 확장 (SIMILAR_TO 연결):")
        with neo4j_driver.session() as session:
            result = session.run("""
                UNWIND $seedIds as seedId
                MATCH (seed:Page {id: seedId})
                OPTIONAL MATCH (seed)-[:SIMILAR_TO]-(related:Page)
                WHERE related.wordCount > 50
                RETURN DISTINCT seed.title as SeedPage,
                       collect(DISTINCT related.title)[0..3] as RelatedPages
            """, seedIds=seed_ids)

            for record in result:
                seed = record["SeedPage"]
                related = record["RelatedPages"]
                if related:
                    print(f"  {seed} → {', '.join(related[:3])}")

    neo4j_driver.close()
    print("\n✅ 하이브리드 검색 데모 완료!")


def main():
    explore_graph_insights()

    # 하이브리드 검색은 선택적 실행
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--hybrid":
        hybrid_search_demo()
    else:
        print("\n💡 하이브리드 검색 데모: python explore_insights.py --hybrid")


if __name__ == "__main__":
    main()
