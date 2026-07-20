import os
import re
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Aladin API Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

TTB_KEY = os.getenv("ALADIN_TTB_KEY", "").strip()

ALADIN_API_BASE = "https://www.aladin.co.kr/ttb/api"
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

def safe_requests_get(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout=(5, 20),
    stream: bool = False,
):
    """
    알라딘에 직접 요청을 먼저 보냅니다.
    Render 환경에서 직접 요청이 막힌 경우에만 프록시를 보조로 시도합니다.
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

    is_ttb_api_request = "/ttb/api/" in url
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

        # TTB API / 이미지 요청은 status 200이면 정상 처리
        # 웹 페이지는 알라딘 상품 또는 검색 HTML인지 검사
        is_valid_direct_response = (
            direct_response.status_code == 200
            and (
                is_ttb_api_request
                or is_aladin_image_request
                or "wproduct" in direct_response.url
                or "wsearchresult" in direct_response.url
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

    # 이미지, TTB API는 프록시를 거치지 않고 마지막 직접 재시도를 합니다.
    if is_ttb_api_request or is_aladin_image_request:
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
                and len(proxy_html) > 2000
                and (
                    "wproduct" in proxy_html
                    or "wsearchresult" in proxy_html
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

    # 마지막 응답을 반환해서 FastAPI endpoint에서 정확한 오류를 확인할 수 있게 합니다.
    return requests.get(
        url,
        params=params,
        headers=request_headers,
        timeout=timeout,
        stream=stream,
        allow_redirects=True,
    )

@app.get("/")
def read_root():
    return {
        "message": "알라딘 검색 API 서버가 정상 작동 중입니다!",
        "ttbKeyConfigured": bool(TTB_KEY),
    }

@app.get("/ping")
def keep_awake():
    return {
        "status": "ok",
        "ttbKeyConfigured": bool(TTB_KEY),
    }

def require_ttb_key():
    if not TTB_KEY:
        raise HTTPException(
            status_code=500,
            detail="ALADIN_TTB_KEY 환경변수가 설정되지 않았습니다.",
        )

def aladin_api_get(endpoint: str, params: dict, timeout: int = 15) -> dict:
    require_ttb_key()

    url = f"{ALADIN_API_BASE}/{endpoint}"

    request_params = {
        "ttbkey": TTB_KEY,
        "output": "js",
        "Version": "20131101",
        **params,
    }

    try:
        response = safe_requests_get(
            url,
            params=request_params,
            headers=DEFAULT_HEADERS,
            timeout=(5, timeout),
        )
    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "알라딘 API 네트워크 오류",
                "reason": str(error),
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "알라딘 API 요청 거부",
                "upstreamStatus": response.status_code,
            },
        )

    try:
        return response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "알라딘 API JSON 파싱 오류",
                "upstreamStatus": response.status_code,
            },
        )

def normalize_cover_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    return (
        url.replace("http://", "https://", 1)
        .replace("coversum", "cover500")
        .replace("cover150", "cover500")
        .replace("cover200", "cover500")
        .replace("/cover/", "/cover500/")
    )

@app.get("/api/search")
def search_books(
    query: str = Query(..., min_length=1, max_length=200),
    max_results: int = Query(10, ge=1, le=50),
    query_type: str = Query("Keyword"),
):
    """
    query_type:
    - Keyword: 제목, 저자, 출판사 전체 검색
    - Author: 저자명 중심 검색
    - Title: 제목 중심 검색
    """
    allowed_query_types = {"Keyword", "Author", "Title"}

    if query_type not in allowed_query_types:
        query_type = "Keyword"

    data = aladin_api_get(
        "ItemSearch.aspx",
        {
            "Query": query.strip(),
            "QueryType": query_type,
            "MaxResults": max_results,
            "start": 1,
            "SearchTarget": "Book",
        },
    )

    for book in data.get("item", []):
        if book.get("cover"):
            book["cover"] = normalize_cover_url(book["cover"])

    return data


@app.get("/api/ttb/search")
def ttb_search_proxy(
    Query_param: str = Query(..., alias="Query", min_length=1, max_length=200),
):
    data = aladin_api_get(
        "ItemSearch.aspx",
        {
            "Query": Query_param.strip(),
            "QueryType": "Keyword",
            "MaxResults": 10,
            "start": 1,
            "SearchTarget": "Book",
        },
    )

    for book in data.get("item", []):
        if book.get("cover"):
            book["cover"] = normalize_cover_url(book["cover"])

    return data

def clean_text(value: str) -> str:
    if not value:
        return ""

    value = re.sub(r"\r", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)

    for word in [
        "접기",
        "펼쳐보기",
        "더보기",
        "책소개 전체",
        "공유하기",
        "보관함",
        "장바구니",
        "바로구매",
        "마이리스트",
    ]:
        value = value.replace(word, "")

    return value.strip()

def extract_text_lines_from_soup(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "noscript", "iframe", "button"]):
        tag.decompose()

    lines = [clean_text(line) for line in soup.get_text("\n").split("\n")]
    return [line for line in lines if line and len(line) > 1]

def extract_section_by_heading(
    lines: list[str],
    start_headings: list[str],
    stop_headings: list[str],
) -> str:
    start_index = -1

    for index, line in enumerate(lines):
        normalized = line.replace(" ", "")

        if any(
            heading.replace(" ", "") in normalized
            for heading in start_headings
            if len(normalized) <= 40
        ):
            start_index = index
            break

    if start_index == -1:
        return ""

    end_index = len(lines)

    for index in range(start_index + 1, len(lines)):
        normalized = lines[index].replace(" ", "")

        if any(
            heading.replace(" ", "") in normalized
            for heading in stop_headings
            if len(normalized) <= 40
        ):
            end_index = index
            break

    return clean_text("\n".join(lines[start_index + 1:end_index]))

def split_phrase_list(text: str) -> list[str]:
    if not text:
        return []

    phrases = []

    for part in re.split(r"\n{2,}", text):
        part = clean_text(part)

        if not part:
            continue

        if len(part) > 600:
            phrases.extend(
                [
                    clean_text(item)
                    for item in part.split("\n")
                    if clean_text(item)
                ]
            )
        else:
            phrases.append(part)

    result = []
    seen = set()

    for phrase in [item for item in phrases if len(item) >= 10]:
        key = phrase[:80]

        if key not in seen:
            seen.add(key)
            result.append(phrase)

    return result[:20]

def extract_by_original_boxes(soup: BeautifulSoup) -> dict:
    texts = {
        "story": "",
        "description": "",
        "phrases": [],
        "mdRecommend": "",
    }

    for box in soup.select(".Ere_prod_mconts_box"):
        title_element = box.select_one(".Ere_prod_mconts_LS")

        if not title_element:
            continue

        title = title_element.get_text(" ", strip=True)

        content_soup = BeautifulSoup(
            str(box.select_one(".Ere_prod_mconts_R") or box),
            "html.parser",
        )

        for unwanted in content_soup(
            ["script", "style", "noscript", "iframe", "button"]
        ):
            unwanted.decompose()

        left_title = content_soup.select_one(".Ere_prod_mconts_LS")
        if left_title:
            left_title.decompose()

        text_content = clean_text(content_soup.get_text("\n"))
        html_content = content_soup.decode_contents().strip()

        if "책소개" in title and "출판사" not in title:
            texts["story"] = text_content or html_content

        elif (
            ("출판사" in title and ("책소개" in title or "상품소개" in title))
            or "출판사 제공" in title
        ):
            texts["description"] = text_content or html_content

        elif "책속에서" in title or "밑줄" in title:
            texts["phrases"] = split_phrase_list(text_content)

        elif "편집장의 선택" in title or "편집장" in title:
            texts["mdRecommend"] = text_content or html_content

    return texts

def resolve_aladin_item_id(
    lookup_id: str,
    title: str = "",
    author: str = "",
    publisher: str = "",
) -> str:
    """
    ISBN, 카카오 ISBN, 카카오 UUID, 이미 알고 있는 알라딘 ItemId를
    알라딘의 실제 숫자 ItemId로 바꿉니다.
    """
    raw = str(lookup_id or "").strip()
    digits = re.sub(r"[^0-9Xx]", "", raw)

    is_isbn13 = len(digits) == 13 and digits.startswith(("978", "979"))
    is_isbn10 = len(digits) == 10

    # 이미 알라딘 ItemId처럼 보이는 숫자라면 그대로 사용.
    # 예: 350996819
    if raw.isdigit() and 6 <= len(raw) <= 12 and not is_isbn10:
        return raw

    # ISBN은 공식 TTB API를 먼저 시도
    if is_isbn13 or is_isbn10:
        try:
            data = aladin_api_get(
                "ItemLookUp.aspx",
                {
                    "ItemId": digits,
                    "ItemIdType": "ISBN13" if is_isbn13 else "ISBN",
                },
            )

            items = data.get("item") or []

            if items and items[0].get("itemId"):
                return str(items[0]["itemId"])

        except Exception as error:
            print("[ALADIN] TTB ISBN lookup failed:", error)

    # TTB API가 없거나 실패할 때, 제목·저자·출판사로 알라딘 웹 검색
    search_queries = []

    title_author_publisher = f"{title} {author} {publisher}".strip()
    title_author = f"{title} {author}".strip()

    if title_author_publisher:
        search_queries.append(title_author_publisher)

    if title_author and title_author not in search_queries:
        search_queries.append(title_author)

    if title and title.strip() not in search_queries:
        search_queries.append(title.strip())

    if is_isbn13 or is_isbn10:
        search_queries.append(digits)

    if raw and raw not in search_queries:
        search_queries.append(raw)

    for search_query in search_queries:
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
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            links = soup.select(
                "a.bo3[href*='ItemId='], "
                "a[href*='wproduct.aspx'][href*='ItemId=']"
            )

            for link in links:
                href = link.get("href") or ""
                match = re.search(r"ItemId=(\d+)", href, re.IGNORECASE)

                if not match:
                    continue

                item_id = match.group(1)
                link_text = link.get_text(" ", strip=True)

                # 제목이 주어진 경우 너무 다른 검색 결과는 피합니다.
                if title:
                    compact_title = re.sub(r"[\s\W_]+", "", title)
                    compact_link_text = re.sub(r"[\s\W_]+", "", link_text)

                    if (
                        len(compact_title) >= 3
                        and compact_title[:3] not in compact_link_text
                    ):
                        continue

                return item_id

            # CSS selector가 변경된 경우 정규식으로 ItemId를 한 번 더 탐색
            match = re.search(
                r"wproduct\.aspx\?ItemId=(\d+)",
                response.text,
                re.IGNORECASE,
            )

            if match:
                return match.group(1)

        except Exception as error:
            print("[ALADIN] Web search fallback failed:", error)

    # 마지막에는 ISBN 또는 원래 값을 반환
    return digits if (is_isbn13 or is_isbn10) else raw

def scrape_aladin_texts(resolved_id: str) -> dict:
    url = f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_id}"

    texts = {
        "story": "",
        "description": "",
        "phrases": [],
        "mdRecommend": "",
        "sourceUrl": url,
    }

    try:
        response = safe_requests_get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=(5, 20),
        )

        if response.status_code != 200:
            return texts

        soup = BeautifulSoup(response.text, "html.parser")

        boxed_texts = extract_by_original_boxes(soup)

        for key in ["story", "description", "mdRecommend", "phrases"]:
            if boxed_texts.get(key):
                texts[key] = boxed_texts[key]

        lines = extract_text_lines_from_soup(soup)

        stop_headings = [
            "책소개",
            "줄거리",
            "책속에서",
            "밑줄긋기",
            "편집장의 선택",
            "출판사 제공 책소개",
            "출판사 리뷰",
            "저자소개",
            "목차",
            "추천글",
            "상품정보",
            "회원리뷰",
        ]

        if not texts["story"]:
            texts["story"] = extract_section_by_heading(
                lines,
                ["책소개", "줄거리"],
                stop_headings,
            )

        if not texts["mdRecommend"]:
            texts["mdRecommend"] = extract_section_by_heading(
                lines,
                ["편집장의 선택"],
                stop_headings,
            )

        if not texts["phrases"]:
            texts["phrases"] = split_phrase_list(
                extract_section_by_heading(
                    lines,
                    ["책속에서", "밑줄긋기"],
                    stop_headings,
                )
            )

        if not texts["description"]:
            texts["description"] = extract_section_by_heading(
                lines,
                [
                    "출판사 제공 상품소개",
                    "출판사 제공 책소개",
                    "출판사 리뷰",
                ],
                stop_headings,
            )

    except Exception as error:
        print("[ALADIN] Text scraping failed:", error)

    return texts

@app.get("/api/ttb/lookup")
def ttb_lookup_proxy(
    ItemId: str = Query(..., min_length=1),
    itemIdType: str = Query("ItemId"),
    OptResult: str = Query(""),
    title: str = Query(""),
    author: str = Query(""),
    publisher: str = Query(""),
):
    try:
        data = aladin_api_get(
            "ItemLookUp.aspx",
            {
                "ItemId": ItemId,
                "ItemIdType": itemIdType,
                "OptResult": OptResult,
            },
        )

        if data.get("item"):
            return data

    except Exception:
        pass

    resolved_id = resolve_aladin_item_id(
        ItemId,
        title,
        author,
        publisher,
    )

    scraped = scrape_aladin_texts(resolved_id)

    item_page = 0

    try:
        response = safe_requests_get(
            f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_id}",
            headers=DEFAULT_HEADERS,
            timeout=(5, 20),
        )

        if response.status_code == 200:
            page_match = re.search(r"(\d+)\s*쪽", response.text)

            if page_match:
                item_page = int(page_match.group(1))

    except Exception:
        pass

    return {
        "item": [
            {
                "itemId": resolved_id,
                "subInfo": {
                    "itemPage": item_page,
                    "story": scraped.get("story", ""),
                    "fulldescription": scraped.get("description", ""),
                    "fulldescription2": scraped.get("description", ""),
                    "mdrecommend": scraped.get("mdRecommend", ""),
                    "phraseList": [
                        {"phrase": phrase}
                        for phrase in scraped.get("phrases", [])
                    ],
                },
            }
        ]
    }

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
    """
    알라딘 이미지 URL을 https URL로 정리합니다.
    coversum / cover200 등 작은 이미지는 cover500으로 바꿉니다.
    """
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
    """
    img 태그와 div 태그에서 src / data-src / style background-image를 찾습니다.
    """
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
    c_front = 표지, c_left = 책등, c_back = 뒷표지
    """
    nodes = soup.select(f".{class_name}")

    if not nodes:
        nodes = soup.select(f'[class*="{class_name}"]')

    for node in nodes:
        # 내부 img 태그 탐색
        for image_tag in node.select("img"):
            image_url = extract_image_from_node(image_tag)

            if image_url:
                return image_url

        # div 자신의 src, data-src, background-image 탐색
        image_url = extract_image_from_node(node)

        if image_url:
            return image_url

    return None

def extract_images_from_raw_html(html: str) -> dict:
    """
    BeautifulSoup가 JS/lazy-loading 이미지 주소를 못 읽는 경우,
    원본 HTML에서 c_front / c_left / c_back 근처의 이미지 URL을 찾습니다.
    """
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
        # class 영역 다음 3,000글자 안에 있는 알라딘 이미지 URL 탐색
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
    """
    상세페이지 전체에서 image.aladin.co.kr 주소를 모두 찾아
    spineflip(책등), letslook(뒷표지), cover(표지)를 구분합니다.
    """
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

        # 책등
        if not found["spine"] and (
            "/spineflip/" in lower_url
            or "/spine/" in lower_url
            or re.search(r"_(?:d|s|sl)\.(?:jpg|jpeg|png)$", lower_url)
        ):
            found["spine"] = url
            continue

        # 뒷표지
        if not found["back"] and (
            "/letslook/" in lower_url
            or re.search(r"_(?:b|bl|wbl)\.(?:jpg|jpeg|png)$", lower_url)
        ):
            found["back"] = url
            continue

        # 앞표지
        if not found["front"] and (
            "/cover500/" in lower_url
            or "/cover200/" in lower_url
            or "/coversum/" in lower_url
            or "/cover/" in lower_url
        ):
            found["front"] = url

    return found

def guess_images_from_front_cover(front_url: Optional[str]) -> dict:
    """
    앞표지 주소가 있을 때 같은 도서의 책등/뒷표지 파일명을 추정합니다.

    예:
    cover500/xxx_1.jpg
    spineflip/xxx_d.jpg
    letslook/xxx_b.jpg
    """
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
    """
    알라딘 '미리보기' 페이지에서 앞/책등/뒷표지를 보조적으로 찾습니다.
    """
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

                # 내부 img 우선
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

@app.get("/api/get-book-images")
def get_book_images(
    item_id: str = Query(..., min_length=1),
    title: str = Query(""),
    author: str = Query(""),
    publisher: str = Query(""),
):
    """
    표지(front), 책등(spine), 뒷표지(back)를 반환합니다.

    알라딘 상세페이지 기준:
    c_front = 앞표지
    c_left = 책등
    c_back = 뒷표지
    """
    resolved_item_id = resolve_aladin_item_id(
        item_id,
        title,
        author,
        publisher,
    )

    url = f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_item_id}"

    images = {
        "front": None,
        "spine": None,
        "back": None,
        "resolvedItemId": resolved_item_id,
        "sourceUrl": url,
    }

    try:
        response = safe_requests_get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=(5, 25),
        )
        response.raise_for_status()

    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "알라딘 도서 상세 페이지를 불러오지 못했습니다.",
                "reason": str(error),
            },
        )

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

    # 브라우저에서 문제 상황을 바로 확인할 수 있도록 debug 값을 같이 반환
    images["debug"] = {
        "htmlLength": len(html),
        "hasAladinProductHtml": "Ere_prod" in html,
        "hasCFront": "c_front" in html,
        "hasCLeft": "c_left" in html,
        "hasCBack": "c_back" in html,
        "imageDomainCount": html.count("image.aladin.co.kr"),
        "responseUrl": response.url,
        "responseStatus": response.status_code,
    }

    print(
        "[BOOK IMAGES]",
        {
            "itemId": resolved_item_id,
            "hasFront": bool(images["front"]),
            "hasSpine": bool(images["spine"]),
            "hasBack": bool(images["back"]),
            "debug": images["debug"],
        },
    )

    return images
