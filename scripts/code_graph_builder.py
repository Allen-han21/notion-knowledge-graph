#!/usr/bin/env python3
"""
코드베이스 Neo4j 그래프 구축
Kidsnote iOS 코드 → CodeFile, Module 노드 + SIMILAR_TO 관계

Phase 5.2: 코드 그래프 빌드
"""

import os
from datetime import datetime
from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from tqdm import tqdm

# Neo4j 설정
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "kidsnote"

# Qdrant 설정
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
CODE_COLLECTION = "kidsnote_ios"

# 유사도 설정
SIMILARITY_THRESHOLD = 0.75  # Notion과 동일한 임계값
TOP_K_SIMILAR = 10


def init_neo4j():
    """Neo4j 드라이버 초기화"""
    print(f"Connecting to Neo4j at {NEO4J_URI}...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    with driver.session() as session:
        result = session.run("RETURN 1 as test")
        result.single()

    print("Neo4j connected successfully")
    return driver


def init_qdrant():
    """Qdrant 클라이언트 초기화"""
    print(f"Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}...")
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # 컬렉션 확인
    collections = [c.name for c in client.get_collections().collections]
    if CODE_COLLECTION not in collections:
        raise ValueError(f"Collection '{CODE_COLLECTION}' not found!")

    info = client.get_collection(CODE_COLLECTION)
    print(f"Qdrant connected. Collection '{CODE_COLLECTION}' has {info.points_count} points")

    return client


def create_code_constraints(driver):
    """코드 관련 제약조건/인덱스 생성"""
    print("Creating constraints and indexes for code nodes...")
    with driver.session() as session:
        try:
            session.run("CREATE CONSTRAINT codefile_path IF NOT EXISTS FOR (c:CodeFile) REQUIRE c.path IS UNIQUE")
        except Exception as e:
            print(f"  Constraint codefile_path: {e}")

        try:
            session.run("CREATE CONSTRAINT module_name IF NOT EXISTS FOR (m:Module) REQUIRE m.name IS UNIQUE")
        except Exception as e:
            print(f"  Constraint module_name: {e}")

        try:
            session.run("CREATE INDEX codefile_name IF NOT EXISTS FOR (c:CodeFile) ON (c.name)")
        except Exception as e:
            print(f"  Index codefile_name: {e}")

    print("Constraints and indexes created")


def get_all_code_points(client) -> list:
    """Qdrant에서 모든 코드 포인트 가져오기"""
    print("Fetching all code points from Qdrant...")
    points = []
    offset = None

    while True:
        result = client.scroll(
            collection_name=CODE_COLLECTION,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=True
        )
        batch, next_offset = result

        if not batch:
            break

        points.extend(batch)
        offset = next_offset

        if next_offset is None:
            break

    print(f"Fetched {len(points)} code points")
    return points


def create_code_nodes(driver, points: list) -> dict:
    """CodeFile 노드 생성"""
    print("\nCreating CodeFile nodes...")
    stats = {"created": 0, "errors": 0, "modules": set()}

    with driver.session() as session:
        for point in tqdm(points, desc="Creating CodeFiles"):
            try:
                payload = point.payload
                module = payload.get("module", "Unknown")
                stats["modules"].add(module)

                session.run("""
                    MERGE (c:CodeFile {path: $path})
                    SET c.name = $name,
                        c.module = $module,
                        c.subpath = $subpath,
                        c.lines = $lines,
                        c.imports = $imports,
                        c.classes = $classes,
                        c.structs = $structs,
                        c.protocols = $protocols
                """,
                    path=payload.get("relative_path", ""),
                    name=payload.get("file_name", ""),
                    module=module,
                    subpath=payload.get("subpath", ""),
                    lines=payload.get("lines", 0),
                    imports=payload.get("imports", []),
                    classes=payload.get("classes", []),
                    structs=payload.get("structs", []),
                    protocols=payload.get("protocols", [])
                )
                stats["created"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    print(f"\nError: {e}")

    return stats


def create_module_nodes(driver, modules: set) -> dict:
    """Module 노드 생성"""
    print("\nCreating Module nodes...")
    stats = {"created": 0}

    with driver.session() as session:
        for module in tqdm(modules, desc="Creating Modules"):
            try:
                session.run("""
                    MERGE (m:Module {name: $name})
                """, name=module)
                stats["created"] += 1
            except Exception as e:
                print(f"Error creating module {module}: {e}")

    return stats


def create_belongs_to_relationships(driver, points: list) -> dict:
    """CodeFile → Module 관계 생성"""
    print("\nCreating BELONGS_TO relationships...")
    stats = {"created": 0}

    with driver.session() as session:
        for point in tqdm(points, desc="Linking to Modules"):
            payload = point.payload
            path = payload.get("relative_path", "")
            module = payload.get("module", "Unknown")

            try:
                result = session.run("""
                    MATCH (c:CodeFile {path: $path})
                    MATCH (m:Module {name: $module})
                    MERGE (c)-[:BELONGS_TO]->(m)
                    RETURN count(*) as created
                """, path=path, module=module)
                if result.single()["created"] > 0:
                    stats["created"] += 1
            except Exception as e:
                pass

    return stats


def create_similarity_edges(driver, client, points: list) -> dict:
    """코드 간 SIMILAR_TO 관계 생성"""
    print(f"\nCreating SIMILAR_TO edges (threshold: {SIMILARITY_THRESHOLD})...")
    stats = {"created": 0, "skipped": 0}

    # 포인트 ID → 경로 매핑
    id_to_path = {str(p.id): p.payload.get("relative_path", "") for p in points}

    with driver.session() as session:
        for point in tqdm(points, desc="Finding Similar Code"):
            try:
                # 유사한 코드 검색 (query_points 사용)
                results = client.query_points(
                    collection_name=CODE_COLLECTION,
                    query=point.vector,
                    limit=TOP_K_SIMILAR + 1,  # 자기 자신 포함
                    with_payload=True
                )

                source_path = point.payload.get("relative_path", "")

                for hit in results.points:
                    # 자기 자신 제외
                    if str(hit.id) == str(point.id):
                        continue

                    # 임계값 미만 제외
                    if hit.score < SIMILARITY_THRESHOLD:
                        stats["skipped"] += 1
                        continue

                    target_path = hit.payload.get("relative_path", "")

                    # 관계 생성 (양방향 중복 방지)
                    if source_path < target_path:  # 알파벳 순으로 한 방향만
                        result = session.run("""
                            MATCH (c1:CodeFile {path: $path1})
                            MATCH (c2:CodeFile {path: $path2})
                            MERGE (c1)-[r:SIMILAR_TO]->(c2)
                            SET r.score = $score
                            RETURN count(*) as created
                        """, path1=source_path, path2=target_path, score=hit.score)

                        if result.single()["created"] > 0:
                            stats["created"] += 1

            except Exception as e:
                if stats["created"] < 3:
                    print(f"\nError: {e}")

    return stats


def analyze_code_graph(driver) -> dict:
    """코드 그래프 분석"""
    print("\nAnalyzing code graph...")
    stats = {}

    with driver.session() as session:
        # CodeFile 노드 수
        result = session.run("MATCH (c:CodeFile) RETURN count(c) as count")
        stats["code_files"] = result.single()["count"]

        # Module 노드 수
        result = session.run("MATCH (m:Module) RETURN count(m) as count")
        stats["modules"] = result.single()["count"]

        # SIMILAR_TO 관계 수
        result = session.run("MATCH ()-[r:SIMILAR_TO]->() RETURN count(r) as count")
        stats["similar_to"] = result.single()["count"]

        # 모듈별 파일 수
        result = session.run("""
            MATCH (c:CodeFile)-[:BELONGS_TO]->(m:Module)
            RETURN m.name as module, count(c) as files
            ORDER BY files DESC
            LIMIT 10
        """)
        stats["top_modules"] = [(r["module"], r["files"]) for r in result]

        # 가장 유사한 코드 쌍
        result = session.run("""
            MATCH (c1:CodeFile)-[r:SIMILAR_TO]->(c2:CodeFile)
            RETURN c1.name as file1, c2.name as file2, r.score as score
            ORDER BY r.score DESC
            LIMIT 5
        """)
        stats["top_similar"] = [(r["file1"], r["file2"], r["score"]) for r in result]

        # 허브 코드 (가장 많은 SIMILAR_TO 관계)
        result = session.run("""
            MATCH (c:CodeFile)-[r:SIMILAR_TO]-()
            RETURN c.name as name, c.module as module, count(r) as connections
            ORDER BY connections DESC
            LIMIT 5
        """)
        stats["hub_files"] = [(r["name"], r["module"], r["connections"]) for r in result]

    return stats


def main():
    start_time = datetime.now()
    print(f"Starting code graph build at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 연결
    driver = init_neo4j()
    client = init_qdrant()

    try:
        # 2. 제약조건 생성
        create_code_constraints(driver)

        # 3. Qdrant에서 데이터 가져오기
        points = get_all_code_points(client)

        # 4. CodeFile 노드 생성
        code_stats = create_code_nodes(driver, points)

        # 5. Module 노드 생성
        module_stats = create_module_nodes(driver, code_stats["modules"])

        # 6. BELONGS_TO 관계 생성
        belongs_stats = create_belongs_to_relationships(driver, points)

        # 7. SIMILAR_TO 관계 생성
        similar_stats = create_similarity_edges(driver, client, points)

        # 8. 분석
        analysis = analyze_code_graph(driver)

        # 결과 출력
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print("\n" + "=" * 60)
        print("Code Graph Build Complete!")
        print("=" * 60)

        print("\n📊 Node Statistics:")
        print(f"  CodeFile: {analysis['code_files']}")
        print(f"  Module: {analysis['modules']}")

        print(f"\n🔗 SIMILAR_TO edges: {analysis['similar_to']}")

        print("\n📁 Top Modules (by file count):")
        for module, files in analysis["top_modules"][:5]:
            print(f"  - {module}: {files} files")

        print("\n🔥 Hub Files (most similar connections):")
        for name, module, conns in analysis["hub_files"]:
            print(f"  - {name} ({module}): {conns} connections")

        print("\n🎯 Most Similar Code Pairs:")
        for f1, f2, score in analysis["top_similar"]:
            print(f"  - {f1} ↔ {f2}: {score:.3f}")

        print(f"\n⏱️ Duration: {duration:.1f} seconds")

        print("\n" + "=" * 60)
        print("Access Neo4j Browser: http://localhost:7474")
        print("=" * 60)

        print("\n✅ Phase 5.2 Complete! (Code Graph)")

    finally:
        driver.close()


if __name__ == "__main__":
    main()
