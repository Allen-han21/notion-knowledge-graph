#!/usr/bin/env python3
"""
Notion 데이터 추출기
- 모든 페이지와 블록 콘텐츠 추출
- 모든 데이터베이스와 아이템 추출
- JSON 파일로 저장
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from notion_client import Client
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 데이터 저장 경로
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_notion_token():
    """환경 변수에서 Notion API 토큰 가져오기"""
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError(
            "NOTION_TOKEN 환경 변수가 설정되지 않았습니다.\n"
            ".env 파일에 NOTION_TOKEN=secret_xxx 형식으로 설정하세요."
        )
    return token


def extract_title(page: dict) -> str:
    """페이지에서 제목 추출"""
    props = page.get("properties", {})

    # title 타입 속성 찾기
    for prop_name, prop_value in props.items():
        if prop_value.get("type") == "title":
            title_arr = prop_value.get("title", [])
            if title_arr:
                return "".join([t.get("plain_text", "") for t in title_arr])

    # Name 속성에서 시도
    if "Name" in props and props["Name"].get("type") == "title":
        title_arr = props["Name"].get("title", [])
        if title_arr:
            return "".join([t.get("plain_text", "") for t in title_arr])

    return "Untitled"


def extract_db_title(db: dict) -> str:
    """데이터베이스에서 제목 추출"""
    title_arr = db.get("title", [])
    if title_arr:
        return "".join([t.get("plain_text", "") for t in title_arr])
    return "Untitled Database"


def blocks_to_text(blocks: list) -> str:
    """블록들을 텍스트로 변환"""
    text_parts = []

    for block in blocks:
        block_type = block.get("type", "")

        # 텍스트 포함 블록 타입들
        text_block_types = [
            "paragraph",
            "heading_1",
            "heading_2",
            "heading_3",
            "bulleted_list_item",
            "numbered_list_item",
            "to_do",
            "toggle",
            "quote",
            "callout",
        ]

        if block_type in text_block_types:
            block_content = block.get(block_type, {})
            rich_text = block_content.get("rich_text", [])
            text = "".join([t.get("plain_text", "") for t in rich_text])
            if text:
                text_parts.append(text)

        # 코드 블록
        elif block_type == "code":
            code_content = block.get("code", {})
            rich_text = code_content.get("rich_text", [])
            text = "".join([t.get("plain_text", "") for t in rich_text])
            if text:
                text_parts.append(f"```\n{text}\n```")

    return "\n".join(text_parts)


def extract_links(blocks: list) -> list:
    """블록에서 다른 페이지 링크 추출"""
    links = []

    for block in blocks:
        block_type = block.get("type", "")
        block_content = block.get(block_type, {})

        # rich_text에서 mention 찾기
        rich_text = block_content.get("rich_text", [])
        for text_item in rich_text:
            if text_item.get("type") == "mention":
                mention = text_item.get("mention", {})
                if mention.get("type") == "page":
                    page_id = mention.get("page", {}).get("id")
                    if page_id:
                        links.append(page_id)

        # link_to_page 블록
        if block_type == "link_to_page":
            link_content = block.get("link_to_page", {})
            if link_content.get("type") == "page_id":
                links.append(link_content.get("page_id"))

    return list(set(links))


def extract_tags(page: dict) -> list:
    """페이지에서 태그/셀렉트 속성 추출"""
    tags = []
    props = page.get("properties", {})

    for prop_name, prop_value in props.items():
        prop_type = prop_value.get("type")

        if prop_type == "multi_select":
            for item in prop_value.get("multi_select", []):
                tags.append({"name": item.get("name"), "color": item.get("color")})

        elif prop_type == "select":
            select_val = prop_value.get("select")
            if select_val:
                tags.append(
                    {"name": select_val.get("name"), "color": select_val.get("color")}
                )

    return tags


class NotionExporter:
    def __init__(self):
        self.notion = Client(auth=get_notion_token())
        self.pages = []
        self.databases = []
        self.stats = {
            "pages_fetched": 0,
            "databases_fetched": 0,
            "blocks_fetched": 0,
            "errors": [],
        }

    def get_all_blocks(self, block_id: str, depth: int = 0) -> list:
        """페이지/블록의 모든 자식 블록을 재귀적으로 가져오기"""
        if depth > 10:  # 무한 재귀 방지
            return []

        blocks = []
        try:
            cursor = None
            while True:
                response = self.notion.blocks.children.list(
                    block_id=block_id, start_cursor=cursor, page_size=100
                )

                for block in response.get("results", []):
                    blocks.append(block)
                    self.stats["blocks_fetched"] += 1

                    # 자식이 있으면 재귀적으로 가져오기
                    if block.get("has_children"):
                        child_blocks = self.get_all_blocks(block["id"], depth + 1)
                        blocks.extend(child_blocks)

                if not response.get("has_more"):
                    break
                cursor = response.get("next_cursor")

                time.sleep(0.1)  # Rate limit 방지

        except Exception as e:
            self.stats["errors"].append(
                {"type": "blocks", "block_id": block_id, "error": str(e)}
            )

        return blocks

    def export_all_pages(self):
        """모든 접근 가능한 페이지 export"""
        print("Fetching all pages...")

        cursor = None
        while True:
            response = self.notion.search(
                filter={"property": "object", "value": "page"},
                start_cursor=cursor,
                page_size=100,
            )

            for page in response.get("results", []):
                self.stats["pages_fetched"] += 1
                print(f"  [{self.stats['pages_fetched']}] {extract_title(page)[:50]}")

                page_data = {
                    "id": page["id"],
                    "title": extract_title(page),
                    "created_time": page["created_time"],
                    "last_edited_time": page["last_edited_time"],
                    "parent": page.get("parent", {}),
                    "properties": page.get("properties", {}),
                    "url": page.get("url", ""),
                    "tags": extract_tags(page),
                }

                # 페이지 내용 가져오기
                blocks = self.get_all_blocks(page["id"])
                page_data["content"] = blocks_to_text(blocks)
                page_data["links"] = extract_links(blocks)
                page_data["word_count"] = len(page_data["content"].split())
                page_data["block_count"] = len(blocks)

                self.pages.append(page_data)
                time.sleep(0.1)  # Rate limit 방지

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        print(f"Total pages fetched: {self.stats['pages_fetched']}")
        return self.pages

    def export_all_databases(self):
        """모든 데이터베이스와 아이템 export"""
        print("\nFetching all databases...")

        # 페이지에서 데이터베이스 정보 추출
        db_ids = set()
        for page in self.pages:
            parent = page.get("parent", {})
            if parent.get("type") == "database_id":
                db_ids.add(parent.get("database_id"))

        for db_id in db_ids:
            try:
                db = self.notion.databases.retrieve(database_id=db_id)
                self.stats["databases_fetched"] += 1
                db_title = extract_db_title(db)
                print(f"  [{self.stats['databases_fetched']}] {db_title[:50]}")

                db_data = {
                    "id": db["id"],
                    "title": db_title,
                    "created_time": db.get("created_time", ""),
                    "last_edited_time": db.get("last_edited_time", ""),
                    "parent": db.get("parent", {}),
                    "properties_schema": db.get("properties", {}),
                    "url": db.get("url", ""),
                    "items": [],
                }

                # 데이터베이스 아이템 조회
                try:
                    items_cursor = None
                    while True:
                        items_response = self.notion.databases.query(
                            database_id=db["id"],
                            start_cursor=items_cursor,
                            page_size=100,
                        )
                        for item in items_response.get("results", []):
                            db_data["items"].append({
                                "id": item["id"],
                                "properties": item.get("properties", {}),
                                "created_time": item["created_time"],
                                "last_edited_time": item["last_edited_time"],
                                "url": item.get("url", ""),
                            })
                        if not items_response.get("has_more"):
                            break
                        items_cursor = items_response.get("next_cursor")
                        time.sleep(0.1)
                except Exception as e:
                    self.stats["errors"].append({
                        "type": "database_items", "db_id": db["id"], "error": str(e)
                    })

                db_data["item_count"] = len(db_data["items"])
                self.databases.append(db_data)
                time.sleep(0.1)
            except Exception as e:
                self.stats["errors"].append({
                    "type": "database_retrieve", "db_id": db_id, "error": str(e)
                })

        print(f"Total databases fetched: {self.stats['databases_fetched']}")
        return self.databases

    def save_to_json(self):
        """추출된 데이터를 JSON 파일로 저장"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 페이지 데이터 저장
        pages_file = DATA_DIR / f"pages_{timestamp}.json"
        with open(pages_file, "w", encoding="utf-8") as f:
            json.dump(self.pages, f, ensure_ascii=False, indent=2)
        print(f"\nPages saved to: {pages_file}")

        # 최신 파일
        latest_pages = DATA_DIR / "pages.json"
        if latest_pages.exists():
            latest_pages.unlink()
        with open(latest_pages, "w", encoding="utf-8") as f:
            json.dump(self.pages, f, ensure_ascii=False, indent=2)

        # 데이터베이스 데이터 저장
        dbs_file = DATA_DIR / f"databases_{timestamp}.json"
        with open(dbs_file, "w", encoding="utf-8") as f:
            json.dump(self.databases, f, ensure_ascii=False, indent=2)
        print(f"Databases saved to: {dbs_file}")

        latest_dbs = DATA_DIR / "databases.json"
        if latest_dbs.exists():
            latest_dbs.unlink()
        with open(latest_dbs, "w", encoding="utf-8") as f:
            json.dump(self.databases, f, ensure_ascii=False, indent=2)

        # 통계 저장
        self.stats["timestamp"] = timestamp
        self.stats["total_pages"] = len(self.pages)
        self.stats["total_databases"] = len(self.databases)
        self.stats["total_db_items"] = sum(db["item_count"] for db in self.databases)
        self.stats["total_words"] = sum(p.get("word_count", 0) for p in self.pages)

        stats_file = DATA_DIR / f"export_stats_{timestamp}.json"
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(self.stats, f, ensure_ascii=False, indent=2)
        print(f"Stats saved to: {stats_file}")

        return self.stats

    def run(self):
        """전체 export 실행"""
        print("=" * 60)
        print("Notion Data Exporter")
        print("=" * 60)

        start_time = time.time()

        self.export_all_pages()
        self.export_all_databases()
        stats = self.save_to_json()

        elapsed = time.time() - start_time

        print("\n" + "=" * 60)
        print("Export Complete!")
        print("=" * 60)
        print(f"Pages: {stats['total_pages']}")
        print(f"Databases: {stats['total_databases']}")
        print(f"Database Items: {stats['total_db_items']}")
        print(f"Total Words: {stats['total_words']:,}")
        print(f"Total Blocks: {stats['blocks_fetched']}")
        print(f"Errors: {len(stats['errors'])}")
        print(f"Time: {elapsed:.1f}s")

        if stats["errors"]:
            print("\nErrors encountered:")
            for err in stats["errors"][:5]:
                print(f"  - {err['type']}: {err['error'][:100]}")

        return stats


if __name__ == "__main__":
    exporter = NotionExporter()
    exporter.run()
