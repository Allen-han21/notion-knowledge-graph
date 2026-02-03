#!/usr/bin/env python3
"""
Neo4j 그래프 구축
Notion 페이지 → Node/Relationship 변환
"""

import json
import os
from pathlib import Path
from datetime import datetime
from neo4j import GraphDatabase
from tqdm import tqdm
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 경로 설정
DATA_DIR = Path(__file__).parent.parent / "data"
PAGES_FILE = DATA_DIR / "pages.json"

# Neo4j 설정
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")

if not NEO4J_PASSWORD:
    raise RuntimeError(
        "NEO4J_PASSWORD 환경 변수가 설정되지 않았습니다.\n"
        ".env 파일에 NEO4J_PASSWORD=your_password 형식으로 설정하세요."
    )


def load_pages() -> list[dict]:
    """pages.json 로드"""
    print(f"Loading pages from {PAGES_FILE}...")
    with open(PAGES_FILE, "r", encoding="utf-8") as f:
        pages = json.load(f)
    print(f"Loaded {len(pages)} pages")
    return pages


def init_neo4j():
    """Neo4j 드라이버 초기화"""
    print(f"Connecting to Neo4j at {NEO4J_URI}...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # 연결 테스트
    with driver.session() as session:
        result = session.run("RETURN 1 as test")
        result.single()

    print("Neo4j connected successfully")
    return driver


def clear_database(driver):
    """기존 데이터 삭제"""
    print("Clearing existing data...")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    print("Database cleared")


def create_constraints(driver):
    """인덱스/제약조건 생성"""
    print("Creating constraints and indexes...")
    with driver.session() as session:
        # Page 노드 유니크 제약조건
        try:
            session.run("CREATE CONSTRAINT page_id IF NOT EXISTS FOR (p:Page) REQUIRE p.id IS UNIQUE")
        except Exception as e:
            print(f"  Constraint page_id: {e}")

        # 인덱스 생성
        try:
            session.run("CREATE INDEX page_title IF NOT EXISTS FOR (p:Page) ON (p.title)")
        except Exception as e:
            print(f"  Index page_title: {e}")

        try:
            session.run("CREATE INDEX page_created IF NOT EXISTS FOR (p:Page) ON (p.createdAt)")
        except Exception as e:
            print(f"  Index page_created: {e}")

    print("Constraints and indexes created")


def create_page_nodes(driver, pages: list[dict]) -> dict:
    """Page 노드 생성"""
    print("\nCreating Page nodes...")
    stats = {"created": 0, "errors": 0}

    with driver.session() as session:
        for page in tqdm(pages, desc="Creating Pages"):
            try:
                created = page.get("created_time", "")
                updated = page.get("last_edited_time", "")

                session.run("""
                    CREATE (p:Page {
                        id: $id,
                        title: $title,
                        url: $url,
                        wordCount: $wordCount,
                        blockCount: $blockCount,
                        createdAt: $created,
                        updatedAt: $updated,
                        parentId: $parentId,
                        parentType: $parentType
                    })
                """,
                    id=page["id"],
                    title=page.get("title", "Untitled"),
                    url=page.get("url", ""),
                    wordCount=page.get("word_count", 0),
                    blockCount=page.get("block_count", 0),
                    created=created,
                    updated=updated,
                    parentId=page.get("parent", {}).get("page_id", "") or page.get("parent", {}).get("database_id", ""),
                    parentType=page.get("parent", {}).get("type", "")
                )
                stats["created"] += 1
            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    print(f"\nError creating page {page.get('id')}: {e}")

    return stats


def create_relationships(driver, pages: list[dict]) -> dict:
    """관계 생성"""
    print("\nCreating relationships...")
    stats = {"child_of": 0, "links_to": 0, "errors": 0}

    # 페이지 ID 집합
    page_ids = {page["id"] for page in pages}

    with driver.session() as session:
        for page in tqdm(pages, desc="Creating Relationships"):
            try:
                # 부모-자식 관계 (CHILD_OF)
                parent_id = page.get("parent", {}).get("page_id", "")
                if parent_id and parent_id in page_ids:
                    result = session.run("""
                        MATCH (child:Page {id: $childId})
                        MATCH (parent:Page {id: $parentId})
                        CREATE (child)-[:CHILD_OF]->(parent)
                        RETURN count(*) as created
                    """, childId=page["id"], parentId=parent_id)
                    if result.single()["created"] > 0:
                        stats["child_of"] += 1

                # 링크 관계 (LINKS_TO)
                for link_id in page.get("links", []):
                    if link_id in page_ids:
                        result = session.run("""
                            MATCH (from:Page {id: $fromId})
                            MATCH (to:Page {id: $toId})
                            CREATE (from)-[:LINKS_TO]->(to)
                            RETURN count(*) as created
                        """, fromId=page["id"], toId=link_id)
                        if result.single()["created"] > 0:
                            stats["links_to"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    print(f"\nError creating relationship for {page.get('id')}: {e}")

    return stats


def create_date_nodes(driver, pages: list[dict]) -> dict:
    """Date 노드 및 CREATED_ON 관계 생성"""
    print("\nCreating Date nodes and relationships...")
    stats = {"dates": 0, "relationships": 0}

    # 고유 날짜 추출
    dates = set()
    for page in pages:
        created = page.get("created_time", "")
        if created:
            date_str = created.split("T")[0]
            dates.add(date_str)

    with driver.session() as session:
        # Date 노드 생성
        for date_str in tqdm(dates, desc="Creating Dates"):
            try:
                parts = date_str.split("-")
                if len(parts) == 3:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    session.run("""
                        CREATE (d:Date {
                            date: $date,
                            year: $year,
                            month: $month,
                            day: $day
                        })
                    """, date=date_str, year=year, month=month, day=day)
                    stats["dates"] += 1
            except Exception:
                pass

        # CREATED_ON 관계 생성
        for page in tqdm(pages, desc="Linking to Dates"):
            created = page.get("created_time", "")
            if created:
                date_str = created.split("T")[0]
                try:
                    result = session.run("""
                        MATCH (p:Page {id: $pageId})
                        MATCH (d:Date {date: $date})
                        CREATE (p)-[:CREATED_ON]->(d)
                        RETURN count(*) as created
                    """, pageId=page["id"], date=date_str)
                    if result.single()["created"] > 0:
                        stats["relationships"] += 1
                except:
                    pass

    return stats


def analyze_graph(driver) -> dict:
    """그래프 분석"""
    print("\nAnalyzing graph...")
    stats = {}

    with driver.session() as session:
        # 노드 수
        result = session.run("MATCH (n) RETURN labels(n)[0] as label, count(*) as count")
        stats["nodes"] = {record["label"]: record["count"] for record in result}

        # 관계 수
        result = session.run("MATCH ()-[r]->() RETURN type(r) as type, count(*) as count")
        stats["relationships"] = {record["type"]: record["count"] for record in result}

        # 허브 노드
        result = session.run("""
            MATCH (p:Page)
            OPTIONAL MATCH (p)-[r]-()
            WITH p, count(r) as connections
            ORDER BY connections DESC
            LIMIT 5
            RETURN p.title as title, connections
        """)
        stats["hub_nodes"] = [(record["title"], record["connections"]) for record in result]

        # 고립된 노드 수
        result = session.run("""
            MATCH (p:Page)
            WHERE NOT (p)-[]-()
            RETURN count(p) as count
        """)
        stats["isolated_pages"] = result.single()["count"]

    return stats


def main():
    start_time = datetime.now()
    print(f"Starting graph build at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 데이터 로드
    pages = load_pages()

    # 2. Neo4j 연결
    driver = init_neo4j()

    try:
        # 3. 기존 데이터 삭제
        clear_database(driver)

        # 4. 제약조건/인덱스 생성
        create_constraints(driver)

        # 5. Page 노드 생성
        page_stats = create_page_nodes(driver, pages)

        # 6. 관계 생성
        rel_stats = create_relationships(driver, pages)

        # 7. Date 노드 생성
        date_stats = create_date_nodes(driver, pages)

        # 8. 그래프 분석
        analysis = analyze_graph(driver)

        # 결과 출력
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print("\n" + "=" * 60)
        print("Graph Build Complete!")
        print("=" * 60)

        print("\n📊 Node Statistics:")
        for label, count in analysis["nodes"].items():
            print(f"  {label}: {count}")

        print("\n🔗 Relationship Statistics:")
        for rel_type, count in analysis["relationships"].items():
            print(f"  {rel_type}: {count}")

        print("\n🏆 Hub Nodes (most connected):")
        for title, connections in analysis["hub_nodes"]:
            print(f"  - {title}: {connections} connections")

        print(f"\n🏝️ Isolated Pages: {analysis['isolated_pages']}")

        print(f"\n⏱️ Duration: {duration:.1f} seconds")

        print("\n" + "=" * 60)
        print("Access Neo4j Browser: http://localhost:7474")
        print("=" * 60)

        print("\n✅ Graph build complete!")

    finally:
        driver.close()


if __name__ == "__main__":
    main()
