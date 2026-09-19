import os
import re
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


# ============================================================
# App
# ============================================================

app = FastAPI(title="Shelfy Book API Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Environment variables / upstream endpoints
# ============================================================

# 메인 도서 API
YES24_API_KEY = os.getenv("YES24_API_KEY", "").strip()
YES24_API_BASE = "https://apis.yes24.com/v1"

# 보조 검색 API
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_BOOK_API_URL = "https://dapi.kakao.com/v3/search/book"

# 알라딘은 공식 TTB API를 더 이상 사용하지 않고,
# 앞표지/책등/뒷표지를 찾기 위한 웹 페이지 조회에만 사용합니다.
ALADIN_WEB_BASE = "https://www.aladin.co.kr"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.aladin.co.kr/",
}


# ============================================================
# Health check
# ============================================================

@app.get("/")
def read_root():
    return {
        "message": "Shelfy 도서 API 서버가 정상 작동 중입니다!",
        "yes24Configured": bool(YES24_API_KEY),
        "kakaoConfigured": bool(KAKAO_REST_API_KEY),
        "searchProvider": "YES24 primary + Kakao fallback",
        "imageProvider": "Aladin web",
    }


@app.get("/ping")
def keep_awake():
    return {
        "status": "ok",
        "yes24Configured": bool(YES24_API_KEY),
        "kakaoConfigured": bool(KAKAO_REST_API_KEY),
    }


# ============================================================
# Generic helpers
# ============================================================

def clean_text(value: str) -> str:
    if not value:
        return ""

    value = re.sub(r"\r", "\n", str(value))
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def digits_only(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", str(value or ""))


def is_isbn13(value: str) -> bool:
    digits = digits_only(value)
    return len(digits) == 13 and digits.isdigit() and digits.startswith(("978", "979"))


def is_isbn10(value: str) -> bool:
    digits = digits_only(value)
    return len(digits) == 10


def compact_compare_text(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "")).casefold()


def matches_query_type(book: dict, query: str, query_type: str) -> bool:
    """
    YES24 상품 검색 API는 title/author 전용 target 파라미터가 없으므로,
    Title/Author 요청일 때 검색 결과를 서버에서 한 번 더 필터링합니다.
    """
    q = compact_compare_text(query)

    if not q:
        return True

    if query_type == "Title":
        return q in compact_compare_text(book.get("title", ""))

    if query_type == "Author":
        return q in compact_compare_text(book.get("author", ""))

    return True


def make_unique_key(book: dict) -> str:
    isbn13 = digits_only(book.get("isbn13", ""))
    if is_isbn13(isbn13):
        return f"isbn13:{isbn13}"

    title = compact_compare_text(book.get("title", ""))
    author = compact_compare_text(book.get("author", ""))
    publisher = compact_compare_text(book.get("publisher", ""))
    return f"meta:{title}|{author}|{publisher}"


# ============================================================
# YES24 API
# ============================================================

def require_yes24_key():
    if not YES24_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="YES24_API_KEY 환경변수가 설정되지 않았습니다.",
        )


def yes24_api_get(
    endpoint: str,
    params: dict,
    timeout: int = 15,
    allow_not_found: bool = False,
) -> dict:
    """YES24 Open API 공통 GET 요청."""
    require_yes24_key()

    url = f"{YES24_API_BASE}/{endpoint.lstrip('/')}"

    try:
        response = requests.get(
            url,
            params=params,
            headers={
                "X-Api-Key": YES24_API_KEY,
                "Accept": "application/json",
            },
            timeout=(5, timeout),
        )
    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "YES24 API 네트워크 오류",
                "reason": str(error),
            },
        )

    # 검색 결과 없음(SEARCH_001)은 정상적인 빈 결과로 취급할 수 있습니다.
    if response.status_code == 404 and allow_not_found:
        return {
            "success": True,
            "message": "검색 결과 없음",
            "errorCode": "SEARCH_001",
            "data": {
                "items": [],
                "currentPage": 1,
                "pageSize": 0,
                "totalCount": 0,
            },
        }

    if response.status_code != 200:
        body = response.text[:700]

        try:
            parsed = response.json()
            body = parsed
        except ValueError:
            pass

        raise HTTPException(
            status_code=502,
            detail={
                "message": "YES24 API 요청 실패",
                "upstreamStatus": response.status_code,
                "upstreamBody": body,
            },
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "YES24 API JSON 파싱 오류",
                "upstreamStatus": response.status_code,
            },
        )

    if not data.get("success"):
        if allow_not_found and data.get("errorCode") == "SEARCH_001":
            return {
                "success": True,
                "message": "검색 결과 없음",
                "errorCode": "SEARCH_001",
                "data": {
                    "items": [],
                    "currentPage": 1,
                    "pageSize": 0,
                    "totalCount": 0,
                },
            }

        raise HTTPException(
            status_code=502,
            detail={
                "message": data.get("message", "YES24 API 오류"),
                "errorCode": data.get("errorCode"),
            },
        )

    return data


def normalize_yes24_book(book: dict) -> Optional[dict]:
    """
    YES24 응답을 기존 알라딘 기반 프론트와 최대한 비슷한 구조로 정규화합니다.

    중요:
    - itemId는 공통 식별자로 ISBN13을 사용합니다.
    - YES24 고유 상품번호는 yes24ItemId에 별도로 저장합니다.
    - 검색 목록의 cover는 YES24 cover URL을 그대로 사용합니다.
    """
    isbn13 = digits_only(book.get("isbn13", ""))

    # ISBN13이 없는 상품은 알라딘 이미지 매칭이 불안정하므로 도서 검색 결과에서 제외합니다.
    if not is_isbn13(isbn13):
        return None

    content = book.get("contentDetail") or {}
    introduction = clean_text(content.get("bookIntroduction") or "")
    summary = clean_text(content.get("bookSummary") or "")
    toc = clean_text(content.get("tableOfContents") or "")

    pages = book.get("pages") or 0

    try:
        pages = int(pages)
    except (TypeError, ValueError):
        pages = 0

    return {
        # 기존 프론트 호환용 공통 ID = ISBN13
        "itemId": isbn13,
        "isbn13": isbn13,
        "isbn": str(book.get("isbn10") or ""),

        # 공급자별 원본 ID
        "yes24ItemId": book.get("itemId"),

        # 기본 메타데이터
        "title": book.get("title") or "",
        "subTitle": book.get("subTitle") or "",
        "author": book.get("author") or "",
        "publisher": book.get("publisher") or "",
        "pubDate": book.get("publishDate") or "",
        "description": introduction or summary,

        # 가격
        "priceStandard": book.get("shopPrice") or 0,
        "priceSales": book.get("salePrice") or 0,

        # 검색 목록에서 바로 사용할 YES24 표지
        "cover": book.get("cover") or "",
        "coverSource": "yes24",

        # 링크 / 상태
        "link": book.get("link") or "",
        "mobileLink": book.get("mobileLink") or "",
        "stockStatus": book.get("itemStatus") or "",
        "adult": str(book.get("adultYn") or "N").upper() == "Y",
        "fixedPrice": str(book.get("fixedBookPriceYn") or "").upper() == "Y",
        "customerReviewRank": book.get("starScore") or 0,
        "salesPoint": book.get("salePoint") or 0,
        "categoryName": book.get("goodsSortNm") or book.get("goodsType") or "도서",
        "mallType": "BOOK",

        # 출처 표시
        "source": "yes24",

        # 상세 페이지 호환용
        "subInfo": {
            "itemPage": pages,
            "story": introduction,
            "fulldescription": summary or introduction,
            "fulldescription2": summary,
            "tableOfContents": toc,
            "mdrecommend": "",
            "phraseList": [],
        },

        # YES24 detail=Y에서 받을 수 있는 추가 정보
        "extra": {
            "originalTitle": book.get("originalTitle"),
            "originalTranslation": book.get("originalTranslation"),
            "itemFormat": book.get("itemFormat"),
            "weight": book.get("weight"),
            "width": book.get("width"),
            "length": book.get("length"),
            "height": book.get("height"),
            "series": book.get("series") or [],
        },
    }


def yes24_search_books(
    query: str,
    max_results: int,
    query_type: str = "Keyword",
) -> tuple[list[dict], int]:
    """YES24를 메인 검색원으로 사용합니다."""
    if not YES24_API_KEY:
        return [], 0

    # Title/Author는 YES24 검색 후 서버 필터링을 하므로 넉넉하게 받습니다.
    fetch_size = max_results
    if query_type in {"Title", "Author"}:
        fetch_size = min(max(max_results * 3, 20), 100)

    data = yes24_api_get(
        "goods/itemList",
        {
            "query": query,
            "category": "BOOK",
            "sort": "DEFAULT",
            "page": 1,
            "pageSize": fetch_size,
            "detail": "N",
        },
        allow_not_found=True,
    )

    payload = data.get("data") or {}
    raw_items = payload.get("items") or []
    total_count = int(payload.get("totalCount") or 0)

    results = []

    for raw_book in raw_items:
        book = normalize_yes24_book(raw_book)

        if not book:
            continue

        if not matches_query_type(book, query, query_type):
            continue

        results.append(book)

        if len(results) >= max_results:
            break

    return results, total_count


def yes24_detail(
    identifier: str,
    search_type: str = "ISBN13",
) -> Optional[dict]:
    if not YES24_API_KEY:
        return None

    if search_type not in {"ISBN13", "ItemId"}:
        search_type = "ISBN13"

    data = yes24_api_get(
        "goods/itemDetail",
        {
            "searchType": search_type,
            "query": str(identifier).strip(),
            "detail": "Y",
        },
        allow_not_found=True,
    )

    items = (data.get("data") or {}).get("items") or []

    if not items:
        return None

    return normalize_yes24_book(items[0])


# ============================================================
# Kakao book search - fallback/discovery only
# ============================================================

def kakao_book_search(
    query: str,
    size: int = 10,
    target: Optional[str] = None,
) -> list[dict]:
    if not KAKAO_REST_API_KEY:
        return []

    params = {
        "query": query,
        "size": min(max(size, 1), 50),
        "page": 1,
        "sort": "accuracy",
    }

    if target:
        params["target"] = target

    try:
        response = requests.get(
            KAKAO_BOOK_API_URL,
            params=params,
            headers={
                "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}",
                "Accept": "application/json",
            },
            timeout=(5, 15),
        )
    except requests.RequestException as error:
        print("[KAKAO] Search request failed:", error)
        return []

    if response.status_code != 200:
        print(
            "[KAKAO] Search request rejected:",
            response.status_code,
            response.text[:300],
        )
        return []

    try:
        return (response.json() or {}).get("documents") or []
    except ValueError:
        return []


def kakao_isbn13(book: dict) -> str:
    isbn_value = str(book.get("isbn") or "")

    for part in isbn_value.split():
        candidate = digits_only(part)
        if is_isbn13(candidate):
            return candidate

    return ""


def normalize_kakao_book(book: dict) -> Optional[dict]:
    """
    YES24 키가 아직 설정되지 않았거나 YES24가 장애일 때를 위한 최소 fallback.
    정상 운영에서는 카카오 결과를 ISBN13으로 YES24 상세 조회하여 YES24 데이터로 바꿉니다.
    """
    isbn13 = kakao_isbn13(book)

    if not isbn13:
        return None

    authors = book.get("authors") or []
    translators = book.get("translators") or []

    return {
        "itemId": isbn13,
        "isbn13": isbn13,
        "isbn": "",
        "yes24ItemId": None,
        "title": book.get("title") or "",
        "subTitle": "",
        "author": ", ".join(authors),
        "publisher": book.get("publisher") or "",
        "pubDate": str(book.get("datetime") or "")[:10],
        "description": clean_text(book.get("contents") or ""),
        "priceStandard": book.get("price") or 0,
        "priceSales": book.get("sale_price") or 0,
        "cover": book.get("thumbnail") or "",
        "coverSource": "kakao-fallback",
        "link": book.get("url") or "",
        "mobileLink": "",
        "stockStatus": book.get("status") or "",
        "adult": False,
        "fixedPrice": False,
        "customerReviewRank": 0,
        "salesPoint": 0,
        "categoryName": "도서",
        "mallType": "BOOK",
        "source": "kakao-fallback",
        "subInfo": {
            "itemPage": 0,
            "story": clean_text(book.get("contents") or ""),
            "fulldescription": clean_text(book.get("contents") or ""),
            "fulldescription2": "",
            "tableOfContents": "",
            "mdrecommend": "",
            "phraseList": [],
        },
        "extra": {
            "translators": translators,
        },
    }


def supplement_with_kakao(
    current_books: list[dict],
    query: str,
    query_type: str,
    max_results: int,
) -> list[dict]:
    """
    카카오는 부족한 검색 결과를 발견하는 용도로 사용합니다.

    YES24 키가 설정되어 있으면:
      Kakao 검색 -> ISBN13 발견 -> YES24 itemDetail -> YES24 cover/메타데이터 반환

    따라서 정상 운영 시 검색창에 표시되는 표지는 YES24 cover로 통일됩니다.
    """
    if len(current_books) >= max_results or not KAKAO_REST_API_KEY:
        return current_books[:max_results]

    target_map = {
        "Title": "title",
        "Author": "person",
    }

    # 중복/ISBN 누락을 고려해 부족한 수보다 넉넉하게 가져옵니다.
    needed = max_results - len(current_books)
    kakao_size = min(max(needed * 4, 10), 50)

    docs = kakao_book_search(
        query=query,
        size=kakao_size,
        target=target_map.get(query_type),
    )

    result = list(current_books)
    seen = {make_unique_key(book) for book in result}

    for doc in docs:
        isbn13 = kakao_isbn13(doc)

        if not isbn13:
            continue

        if f"isbn13:{isbn13}" in seen:
            continue

        normalized = None

        # 핵심: 카카오가 발견한 도서라도 최종 표지/정보는 YES24로 다시 조회합니다.
        if YES24_API_KEY:
            try:
                normalized = yes24_detail(isbn13, "ISBN13")
            except HTTPException as error:
                print("[YES24] Kakao hydration failed:", error.detail)
                normalized = None

            # YES24에 없는 책은 정상 운영에서는 검색 결과에서 제외하여
            # 검색 목록 cover가 YES24 이미지로 유지되게 합니다.
            if not normalized:
                continue
        else:
            # 아직 YES24 키를 발급받기 전 개발 테스트용 fallback
            normalized = normalize_kakao_book(doc)

        if not normalized:
            continue

        if not matches_query_type(normalized, query, query_type):
            continue

        key = make_unique_key(normalized)

        if key in seen:
            continue

        seen.add(key)
        result.append(normalized)

        if len(result) >= max_results:
            break

    return result[:max_results]


def search_books_internal(
    query: str,
    max_results: int,
    query_type: str,
) -> dict:
    allowed_query_types = {"Keyword", "Author", "Title"}
    if query_type not in allowed_query_types:
        query_type = "Keyword"

    query = query.strip()

    if not YES24_API_KEY and not KAKAO_REST_API_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "YES24_API_KEY 또는 KAKAO_REST_API_KEY 중 하나 이상이 필요합니다. "
                "운영 환경에서는 YES24_API_KEY 설정을 권장합니다."
            ),
        )

    yes24_error = None
    books: list[dict] = []
    yes24_total = 0

    if YES24_API_KEY:
        try:
            books, yes24_total = yes24_search_books(
                query=query,
                max_results=max_results,
                query_type=query_type,
            )
        except HTTPException as error:
            # YES24 일시 장애 시 카카오 검색으로라도 서비스가 죽지 않도록 합니다.
            yes24_error = error.detail
            print("[YES24] Search failed, trying Kakao fallback:", yes24_error)
            books = []

    books = supplement_with_kakao(
        current_books=books,
        query=query,
        query_type=query_type,
        max_results=max_results,
    )

    # YES24가 실패했고 카카오도 결과가 없으면 원래 YES24 오류를 사용자에게 전달합니다.
    if not books and yes24_error and not KAKAO_REST_API_KEY:
        raise HTTPException(status_code=502, detail=yes24_error)

    return {
        # 기존 Aladin 응답과 비슷하게 유지
        "item": books,
        "totalResults": yes24_total if yes24_total else len(books),
        "startIndex": 1,
        "itemsPerPage": len(books),
        "query": query,
        "queryType": query_type,
        "searchProvider": "yes24+kakao" if KAKAO_REST_API_KEY else "yes24",
        "coverPolicy": "YES24 cover preferred",
    }


# ============================================================
# Public search endpoints
# ============================================================

@app.get("/api/search")
def search_books(
    query: str = Query(..., min_length=1, max_length=200),
    max_results: int = Query(10, ge=1, le=50),
    query_type: str = Query("Keyword"),
):
    return search_books_internal(query, max_results, query_type)


# 기존 프론트가 /api/ttb/search?Query=... 를 사용해도 깨지지 않도록 유지합니다.
@app.get("/api/ttb/search")
def ttb_search_proxy(
    Query_param: str = Query(..., alias="Query", min_length=1, max_length=200),
):
    return search_books_internal(Query_param, 10, "Keyword")


# ============================================================
# Book detail endpoint
# ============================================================

def find_yes24_book_by_metadata(
    title: str = "",
    author: str = "",
    publisher: str = "",
) -> Optional[dict]:
    """
    과거에 저장한 Aladin ItemId처럼 YES24에서 직접 해석할 수 없는 ID가 들어왔을 때
    제목/저자/출판사를 이용해 YES24 도서를 다시 찾습니다.
    """
    if not YES24_API_KEY or not title.strip():
        return None

    try:
        data = yes24_api_get(
            "goods/itemList",
            {
                "query": title.strip(),
                "category": "BOOK",
                "sort": "DEFAULT",
                "page": 1,
                "pageSize": 20,
                "detail": "N",
            },
            allow_not_found=True,
        )
    except HTTPException:
        return None

    items = (data.get("data") or {}).get("items") or []
    wanted_title = compact_compare_text(title)
    wanted_author = compact_compare_text(author)
    wanted_publisher = compact_compare_text(publisher)

    best = None
    best_score = -1

    for raw_book in items:
        normalized = normalize_yes24_book(raw_book)
        if not normalized:
            continue

        score = 0
        found_title = compact_compare_text(normalized.get("title", ""))
        found_author = compact_compare_text(normalized.get("author", ""))
        found_publisher = compact_compare_text(normalized.get("publisher", ""))

        if wanted_title:
            if wanted_title == found_title:
                score += 6
            elif wanted_title in found_title or found_title in wanted_title:
                score += 4
            else:
                continue

        if wanted_author and (wanted_author in found_author or found_author in wanted_author):
            score += 2

        if wanted_publisher and (
            wanted_publisher in found_publisher or found_publisher in wanted_publisher
        ):
            score += 1

        if score > best_score:
            best = normalized
            best_score = score

    if not best:
        return None

    # 검색 결과는 detail=N이므로 ISBN13으로 상세정보를 한 번 더 가져옵니다.
    isbn13 = best.get("isbn13") or ""
    if is_isbn13(isbn13):
        try:
            detailed = yes24_detail(isbn13, "ISBN13")
            if detailed:
                return detailed
        except HTTPException:
            pass

    return best


def resolve_yes24_lookup_type(
    item_id: str,
    item_id_type: str = "",
) -> tuple[str, str]:
    raw = str(item_id or "").strip()

    if raw.upper().startswith("YES24:"):
        return "ItemId", raw.split(":", 1)[1].strip()

    digits = digits_only(raw)

    if is_isbn13(digits):
        return "ISBN13", digits

    # 기존 클라이언트가 ItemId를 보내는 경우를 위해 YES24 상품번호도 지원합니다.
    if digits.isdigit() and digits:
        return "ItemId", digits

    # 명시값이 유효하면 사용
    if item_id_type in {"ISBN13", "ItemId"}:
        return item_id_type, raw

    return "ISBN13", raw


def kakao_detail_by_isbn(isbn13: str) -> Optional[dict]:
    if not KAKAO_REST_API_KEY or not is_isbn13(isbn13):
        return None

    docs = kakao_book_search(
        query=isbn13,
        size=5,
        target="isbn",
    )

    for doc in docs:
        if kakao_isbn13(doc) == isbn13:
            return normalize_kakao_book(doc)

    return None


@app.get("/api/ttb/lookup")
def ttb_lookup_proxy(
    ItemId: str = Query(..., min_length=1),
    itemIdType: str = Query("ItemId"),
    OptResult: str = Query(""),
    title: str = Query(""),
    author: str = Query(""),
    publisher: str = Query(""),
):
    """
    이름은 프론트 호환 때문에 /api/ttb/lookup으로 유지하지만,
    실제 상세 정보는 YES24에서 가져옵니다.
    """
    del OptResult  # 기존 파라미터 호환용. YES24에서는 사용하지 않음.

    search_type, identifier = resolve_yes24_lookup_type(ItemId, itemIdType)

    book = None

    if YES24_API_KEY:
        try:
            book = yes24_detail(identifier, search_type)
        except HTTPException as error:
            print("[YES24] Detail lookup failed:", error.detail)

    # ItemId로 조회했는데 결과가 없고 실제 값이 ISBN13처럼 보인다면 ISBN13으로 재시도
    if not book and YES24_API_KEY and is_isbn13(ItemId):
        try:
            book = yes24_detail(digits_only(ItemId), "ISBN13")
        except HTTPException:
            pass

    # 기존 DB에 저장된 Aladin ItemId는 YES24 ItemId와 숫자 형태가 겹칠 수 있습니다.
    # YES24 직접 조회가 실패하면 함께 전달된 제목/저자/출판사로 재검색합니다.
    if not book and YES24_API_KEY and title.strip():
        book = find_yes24_book_by_metadata(
            title=title,
            author=author,
            publisher=publisher,
        )

    # YES24를 못 쓰는 개발 단계/장애 상황에서는 카카오 ISBN 상세로 최소 보완
    if not book and is_isbn13(ItemId):
        book = kakao_detail_by_isbn(digits_only(ItemId))

    if not book:
        return {"item": []}

    return {"item": [book]}


# 새 코드에서 의미가 더 분명한 별칭도 함께 제공합니다.
@app.get("/api/book-detail")
def book_detail(
    isbn13: str = Query(..., min_length=13, max_length=20),
):
    clean_isbn = digits_only(isbn13)

    if not is_isbn13(clean_isbn):
        raise HTTPException(status_code=400, detail="올바른 ISBN13이 아닙니다.")

    book = None

    if YES24_API_KEY:
        book = yes24_detail(clean_isbn, "ISBN13")

    if not book:
        book = kakao_detail_by_isbn(clean_isbn)

    if not book:
        raise HTTPException(status_code=404, detail="도서 상세 정보를 찾지 못했습니다.")

    return {"item": [book]}


# ============================================================
# Aladin web helpers - images only
# ============================================================

def safe_requests_get(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout=(5, 20),
    stream: bool = False,
):
    """
    알라딘 웹 페이지/이미지에 직접 요청을 먼저 보냅니다.
    Render 환경에서 HTML 페이지 직접 요청이 막힌 경우에만 공개 프록시를 보조로 시도합니다.

    주의: YES24/Kakao API 호출에는 이 함수를 사용하지 않습니다.
    """
    request_headers = {
        **DEFAULT_HEADERS,
        **(headers or {}),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json;q=0.8,image/avif,image/webp,image/apng,*/*;q=0.7"
        ),
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }

    is_aladin_image_request = "image.aladin.co.kr/" in url

    try:
        direct_response = requests.get(
            url,
            params=params,
            headers=request_headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=True,
        )

        direct_html = "" if stream else direct_response.text

        is_valid_direct_response = (
            direct_response.status_code == 200
            and (
                is_aladin_image_request
                or "wproduct" in direct_response.url
                or "wsearchresult" in direct_response.url
                or "wletslookViewer" in direct_response.url
                or "Ere_prod" in direct_html
                or "c_front" in direct_html
                or "c_left" in direct_html
                or "알라딘" in direct_html
            )
        )

        if is_valid_direct_response:
            print("[ALADIN] Direct request success:", direct_response.url)
            return direct_response

        print(
            "[ALADIN] Direct request unexpected:",
            {
                "status": direct_response.status_code,
                "url": direct_response.url,
                "htmlLength": len(direct_html),
            },
        )

    except requests.RequestException as error:
        print("[ALADIN] Direct request failed:", error)

    # 이미지 요청은 프록시를 거치지 않고 마지막 직접 재시도를 합니다.
    if is_aladin_image_request:
        return requests.get(
            url,
            params=params,
            headers=request_headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=True,
        )

    if params:
        req_url = url + "?" + urllib.parse.urlencode(params)
    else:
        req_url = url

    proxy_urls = [
        "https://api.allorigins.win/raw?url="
        + urllib.parse.quote(req_url, safe=""),
        "https://api.codetabs.com/v1/proxy?quest="
        + urllib.parse.quote(req_url, safe=""),
        "https://corsproxy.io/?"
        + urllib.parse.quote(req_url, safe=""),
    ]

    for proxy_url in proxy_urls:
        try:
            proxy_response = requests.get(
                proxy_url,
                headers=request_headers,
                timeout=timeout,
                stream=stream,
                allow_redirects=True,
            )

            proxy_html = "" if stream else proxy_response.text

            is_valid_proxy_response = (
                proxy_response.status_code == 200
                and len(proxy_html) > 1000
                and (
                    "wproduct" in proxy_html
                    or "wsearchresult" in proxy_html
                    or "wletslookViewer" in proxy_html
                    or "Ere_prod" in proxy_html
                    or "c_front" in proxy_html
                    or "c_left" in proxy_html
                    or "알라딘" in proxy_html
                )
            )

            if is_valid_proxy_response:
                print("[ALADIN] Proxy request success:", proxy_url[:70])
                return proxy_response

            print(
                "[ALADIN] Proxy request unexpected:",
                {
                    "status": proxy_response.status_code,
                    "htmlLength": len(proxy_html),
                },
            )

        except requests.RequestException as error:
            print("[ALADIN] Proxy request failed:", error)

    return requests.get(
        url,
        params=params,
        headers=request_headers,
        timeout=timeout,
        stream=stream,
        allow_redirects=True,
    )


def aladin_search_item_id(
    search_query: str,
    title: str = "",
) -> str:
    if not search_query:
        return ""

    try:
        response = safe_requests_get(
            f"{ALADIN_WEB_BASE}/search/wsearchresult.aspx",
            params={
                "SearchTarget": "Book",
                "SearchWord": search_query,
            },
            headers=DEFAULT_HEADERS,
            timeout=(5, 20),
        )

        if response.status_code != 200:
            return ""

        soup = BeautifulSoup(response.text, "html.parser")

        links = soup.select(
            "a.bo3[href*='ItemId='], "
            "a[href*='wproduct.aspx'][href*='ItemId=']"
        )

        compact_title = compact_compare_text(title)

        for link in links:
            href = link.get("href") or ""
            match = re.search(r"ItemId=(\d+)", href, re.IGNORECASE)

            if not match:
                continue

            # 제목이 주어진 경우 너무 다른 결과를 피합니다.
            if compact_title:
                link_text = compact_compare_text(link.get_text(" ", strip=True))
                prefix = compact_title[: min(4, len(compact_title))]

                if prefix and prefix not in link_text:
                    continue

            return match.group(1)

        # CSS selector가 바뀐 경우 HTML 전체에서 ItemId를 한 번 더 탐색
        match = re.search(
            r"wproduct\.aspx\?ItemId=(\d+)",
            response.text,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    except Exception as error:
        print("[ALADIN] Web search failed:", error)

    return ""


def get_yes24_isbn_from_item_id(yes24_item_id: str) -> str:
    if not YES24_API_KEY or not str(yes24_item_id).isdigit():
        return ""

    try:
        book = yes24_detail(str(yes24_item_id), "ItemId")
    except HTTPException:
        return ""

    if not book:
        return ""

    isbn13 = digits_only(book.get("isbn13", ""))
    return isbn13 if is_isbn13(isbn13) else ""


def validate_direct_aladin_item_id(
    item_id: str,
    title: str = "",
) -> bool:
    if not str(item_id).isdigit():
        return False

    try:
        response = safe_requests_get(
            f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={item_id}",
            headers=DEFAULT_HEADERS,
            timeout=(5, 15),
        )

        if response.status_code != 200:
            return False

        html = response.text

        if "Ere_prod" not in html and "c_front" not in html and "알라딘" not in html:
            return False

        if title:
            soup = BeautifulSoup(html, "html.parser")
            page_text = compact_compare_text(soup.get_text(" ", strip=True))
            wanted = compact_compare_text(title)
            prefix = wanted[: min(5, len(wanted))]

            if prefix and prefix not in page_text:
                return False

        return True

    except Exception:
        return False


def resolve_aladin_item_id(
    lookup_id: str,
    title: str = "",
    author: str = "",
    publisher: str = "",
) -> str:
    """
    ISBN13/ISBN10/YES24 ItemId/기존 Aladin ItemId를
    알라딘 웹 상품 페이지의 숫자 ItemId로 변환합니다.

    이전 코드와 달리 Aladin TTB API는 전혀 호출하지 않습니다.
    """
    raw = str(lookup_id or "").strip()

    if not raw and not title:
        return ""

    # 명시적으로 ALADIN:123456789 형식이 오면 바로 검증
    if raw.upper().startswith("ALADIN:"):
        candidate = raw.split(":", 1)[1].strip()
        return candidate if validate_direct_aladin_item_id(candidate, title) else ""

    # YES24:12345678 형식 지원
    if raw.upper().startswith("YES24:"):
        yes_id = raw.split(":", 1)[1].strip()
        isbn13 = get_yes24_isbn_from_item_id(yes_id)
        if isbn13:
            raw = isbn13

    digits = digits_only(raw)
    search_queries: list[str] = []

    # ISBN이면 알라딘 웹 검색에서 ISBN을 최우선으로 사용합니다.
    if is_isbn13(digits) or is_isbn10(digits):
        search_queries.append(digits)

    # 숫자 상품번호인데 ISBN이 아니면 YES24 ItemId일 가능성을 먼저 확인합니다.
    elif digits.isdigit() and 6 <= len(digits) <= 12:
        isbn13 = get_yes24_isbn_from_item_id(digits)
        if isbn13:
            search_queries.append(isbn13)

    title_author_publisher = f"{title} {author} {publisher}".strip()
    title_author = f"{title} {author}".strip()

    for value in [title_author_publisher, title_author, title]:
        value = value.strip()
        if value and value not in search_queries:
            search_queries.append(value)

    # ISBN/메타데이터 기반 검색을 우선 수행
    for search_query in search_queries:
        item_id = aladin_search_item_id(search_query, title=title)
        if item_id:
            return item_id

    # 마지막 호환성 처리:
    # 과거 프론트가 실제 Aladin ItemId를 넘긴 경우에만 직접 상품 페이지를 검증해서 사용합니다.
    if digits.isdigit() and 6 <= len(digits) <= 12:
        if validate_direct_aladin_item_id(digits, title=title):
            return digits

    return ""


# ============================================================
# Aladin image extraction
# ============================================================

def check_url(url: str) -> bool:
    """이미지 URL이 실제로 존재하는지 확인합니다."""
    try:
        response = safe_requests_get(
            url,
            headers=DEFAULT_HEADERS,
            stream=True,
            timeout=(3, 8),
        )
        return response.status_code == 200

    except requests.RequestException:
        return False


def normalize_aladin_image_url(src: Optional[str]) -> Optional[str]:
    """알라딘 이미지 URL을 https/고해상도 형태로 정리합니다."""
    if not src:
        return None

    src = str(src).strip().strip('"').strip("'").replace("&amp;", "&")

    if src.startswith("//"):
        src = "https:" + src

    elif src.startswith("/"):
        src = ALADIN_WEB_BASE + src

    elif src.startswith("http://"):
        src = src.replace("http://", "https://", 1)

    if "image.aladin.co.kr/" not in src:
        return None

    src = src.replace("/coversum/", "/cover500/")
    src = src.replace("/cover150/", "/cover500/")
    src = src.replace("/cover200/", "/cover500/")
    src = src.replace("/cover/", "/cover500/")

    return src


def extract_image_from_node(node) -> Optional[str]:
    if not node:
        return None

    for attribute in [
        "src",
        "data-src",
        "data-original",
        "data-lazy",
        "data-url",
        "data-image",
    ]:
        value = node.get(attribute)

        if value:
            image_url = normalize_aladin_image_url(value)

            if image_url:
                return image_url

    style = node.get("style", "")

    match = re.search(
        r"url\(\s*['\"]?([^'\"\)]+)['\"]?\s*\)",
        style,
        re.IGNORECASE,
    )

    if match:
        return normalize_aladin_image_url(match.group(1))

    return None


def extract_class_image(
    soup: BeautifulSoup,
    class_name: str,
) -> Optional[str]:
    """
    알라딘 상품 상세의 c_front / c_left / c_back 영역에서 이미지 추출.
    c_front = 앞표지, c_left = 책등, c_back = 뒷표지
    """
    nodes = soup.select(f".{class_name}")

    if not nodes:
        nodes = soup.select(f'[class*="{class_name}"]')

    for node in nodes:
        for image_tag in node.select("img"):
            image_url = extract_image_from_node(image_tag)

            if image_url:
                return image_url

        image_url = extract_image_from_node(node)

        if image_url:
            return image_url

    return None


def extract_images_from_raw_html(html: str) -> dict:
    found = {
        "front": None,
        "spine": None,
        "back": None,
    }

    class_patterns = {
        "front": "c_front",
        "spine": "c_left",
        "back": "c_back",
    }

    for image_type, class_name in class_patterns.items():
        pattern = (
            rf'class=["\'][^"\']*{class_name}[^"\']*["\'][^>]*>'
            rf'[\s\S]{{0,3000}}?'
            rf'((?:https?:)?//image\.aladin\.co\.kr/[^"\'<>\s]+?\.(?:jpg|jpeg|png))'
        )

        match = re.search(pattern, html, re.IGNORECASE)

        if match:
            found[image_type] = normalize_aladin_image_url(match.group(1))

    return found


def find_images_in_all_aladin_urls(html: str) -> dict:
    found = {
        "front": None,
        "spine": None,
        "back": None,
    }

    urls = re.findall(
        r'(?:https?:)?//image\.aladin\.co\.kr/[^"\'<>\s\\]+?\.(?:jpg|jpeg|png)',
        html,
        re.IGNORECASE,
    )

    for raw_url in urls:
        url = normalize_aladin_image_url(raw_url)

        if not url:
            continue

        lower_url = url.lower()

        if not found["spine"] and (
            "/spineflip/" in lower_url
            or "/spine/" in lower_url
            or re.search(r"_(?:d|s|sl)\.(?:jpg|jpeg|png)$", lower_url)
        ):
            found["spine"] = url
            continue

        if not found["back"] and (
            "/letslook/" in lower_url
            or re.search(r"_(?:b|bl|wbl)\.(?:jpg|jpeg|png)$", lower_url)
        ):
            found["back"] = url
            continue

        if not found["front"] and (
            "/cover500/" in lower_url
            or "/cover200/" in lower_url
            or "/coversum/" in lower_url
            or "/cover/" in lower_url
        ):
            found["front"] = url

    return found


def guess_images_from_front_cover(front_url: Optional[str]) -> dict:
    found = {
        "spine": None,
        "back": None,
    }

    if not front_url:
        return found

    match = re.search(
        r"(https://image\.aladin\.co\.kr/product/\d+/\d+/)"
        r"(?:cover500|cover200|coversum|cover|spineflip|spine|letslook)/"
        r"([^/]+?)(?:_\d+)?\.(?:jpg|jpeg|png)",
        front_url,
        re.IGNORECASE,
    )

    if not match:
        return found

    base_url = match.group(1)
    file_name = re.sub(r"_\d+$", "", match.group(2))

    spine_candidates = [
        f"{base_url}spineflip/{file_name}_d.jpg",
        f"{base_url}spineflip/{file_name}_s.jpg",
        f"{base_url}spine/{file_name}_d.jpg",
        f"{base_url}spine/{file_name}_s.jpg",
    ]

    back_candidates = [
        f"{base_url}letslook/{file_name}_b.jpg",
        f"{base_url}letslook/{file_name}_bl.jpg",
        f"{base_url}letslook/{file_name}_wbl.jpg",
    ]

    for candidate in spine_candidates:
        if check_url(candidate):
            found["spine"] = candidate
            break

    for candidate in back_candidates:
        if check_url(candidate):
            found["back"] = candidate
            break

    return found


def extract_preview_page_images(item_id: str, headers: dict) -> dict:
    found = {
        "front": None,
        "spine": None,
        "back": None,
    }

    try:
        response = safe_requests_get(
            f"{ALADIN_WEB_BASE}/shop/book/wletslookViewer.aspx?ItemId={item_id}",
            headers=headers,
            timeout=(5, 20),
        )

        if response.status_code != 200:
            return found

        soup = BeautifulSoup(response.text, "html.parser")

        selectors = {
            "front": [".pageType2.rightpage", ".rightpage"],
            "spine": [".bookspine", ".spine"],
            "back": [".pageType3.leftpage", ".leftpage"],
        }

        for image_type, selector_list in selectors.items():
            for selector in selector_list:
                node = soup.select_one(selector)

                if not node:
                    continue

                image_tag = node.select_one("img")

                if image_tag:
                    image_url = extract_image_from_node(image_tag)
                else:
                    image_url = extract_image_from_node(node)

                if image_url:
                    found[image_type] = image_url
                    break

    except Exception as error:
        print("[ALADIN] Preview image extraction failed:", error)

    return found


def yes24_cover_for_identifier(item_id: str) -> Optional[str]:
    """알라딘 앞표지 추출 실패 시 사용할 YES24 앞표지 fallback."""
    if not YES24_API_KEY:
        return None

    raw = str(item_id or "").strip()
    digits = digits_only(raw)

    try:
        if is_isbn13(digits):
            book = yes24_detail(digits, "ISBN13")
        elif digits.isdigit():
            book = yes24_detail(digits, "ItemId")
        else:
            book = None
    except HTTPException:
        book = None

    if not book:
        return None

    return book.get("cover") or None


@app.get("/api/get-book-images")
def get_book_images(
    item_id: str = Query(..., min_length=1),
    title: str = Query(""),
    author: str = Query(""),
    publisher: str = Query(""),
):
    """
    책 상세/책장 표현용 이미지 엔드포인트.

    - 검색 목록: /api/search 응답의 YES24 cover를 그대로 사용
    - 이 엔드포인트: 알라딘 웹에서 앞표지(front), 책등(spine), 뒷표지(back)를 찾음
    - 알라딘 앞표지를 못 찾으면 front만 YES24 cover로 fallback
    """
    yes24_fallback_cover = yes24_cover_for_identifier(item_id)

    resolved_item_id = resolve_aladin_item_id(
        item_id,
        title,
        author,
        publisher,
    )

    images = {
        "front": None,
        "spine": None,
        "back": None,
        "resolvedItemId": resolved_item_id or None,
        "sourceUrl": (
            f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_item_id}"
            if resolved_item_id
            else None
        ),
        "frontSource": None,
    }

    # 알라딘 상품을 못 찾은 경우에도 YES24 표지는 반환합니다.
    if not resolved_item_id:
        images["front"] = yes24_fallback_cover
        images["frontSource"] = "yes24-fallback" if yes24_fallback_cover else None
        images["debug"] = {
            "message": "알라딘 ItemId를 찾지 못했습니다.",
            "requestedId": item_id,
            "hasYes24FallbackCover": bool(yes24_fallback_cover),
        }
        return images

    url = f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_item_id}"

    try:
        response = safe_requests_get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=(5, 25),
        )
        response.raise_for_status()

    except requests.RequestException as error:
        # 이미지 서버가 일시 실패해도 검색/상세 페이지까지 실패시키지 않습니다.
        images["front"] = yes24_fallback_cover
        images["frontSource"] = "yes24-fallback" if yes24_fallback_cover else None
        images["debug"] = {
            "message": "알라딘 도서 상세 페이지를 불러오지 못했습니다.",
            "reason": str(error),
            "hasYes24FallbackCover": bool(yes24_fallback_cover),
        }
        return images

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    # 1. 실제 상세페이지 클래스에서 우선 추출
    images["front"] = extract_class_image(soup, "c_front")
    images["spine"] = extract_class_image(soup, "c_left")
    images["back"] = extract_class_image(soup, "c_back")

    # 2. lazy loading / script 포함 원본 HTML에서 추출
    raw_images = extract_images_from_raw_html(html)

    for key in ["front", "spine", "back"]:
        if not images[key] and raw_images.get(key):
            images[key] = raw_images[key]

    # 3. HTML 전체 이미지 URL에서 폴더명으로 추출
    all_images = find_images_in_all_aladin_urls(html)

    for key in ["front", "spine", "back"]:
        if not images[key] and all_images.get(key):
            images[key] = all_images[key]

    # 4. 앞표지 기반으로 spineflip / letslook URL 추정
    if not images["spine"] or not images["back"]:
        guessed_images = guess_images_from_front_cover(images["front"])

        if not images["spine"] and guessed_images.get("spine"):
            images["spine"] = guessed_images["spine"]

        if not images["back"] and guessed_images.get("back"):
            images["back"] = guessed_images["back"]

    # 5. 알라딘 미리보기 페이지에서 마지막 시도
    if not images["front"] or not images["spine"] or not images["back"]:
        preview_images = extract_preview_page_images(
            resolved_item_id,
            DEFAULT_HEADERS,
        )

        for key in ["front", "spine", "back"]:
            if not images[key] and preview_images.get(key):
                images[key] = preview_images[key]

    # 6. 알라딘 앞표지만 못 찾았을 경우 YES24 cover fallback
    if images["front"]:
        images["frontSource"] = "aladin"
    elif yes24_fallback_cover:
        images["front"] = yes24_fallback_cover
        images["frontSource"] = "yes24-fallback"

    images["debug"] = {
        "htmlLength": len(html),
        "hasAladinProductHtml": "Ere_prod" in html,
        "hasCFront": "c_front" in html,
        "hasCLeft": "c_left" in html,
        "hasCBack": "c_back" in html,
        "imageDomainCount": html.count("image.aladin.co.kr"),
        "responseUrl": response.url,
        "responseStatus": response.status_code,
        "hasYes24FallbackCover": bool(yes24_fallback_cover),
    }

    print(
        "[BOOK IMAGES]",
        {
            "requestedId": item_id,
            "aladinItemId": resolved_item_id,
            "hasFront": bool(images["front"]),
            "hasSpine": bool(images["spine"]),
            "hasBack": bool(images["back"]),
            "frontSource": images["frontSource"],
        },
    )

    return images
