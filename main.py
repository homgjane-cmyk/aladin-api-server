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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.aladin.co.kr/",
}

# 💡 다중 프록시 터널 (서버 하나가 죽어도 다른 서버로 자동 우회)
def safe_requests_get(url: str, params: dict = None, headers: dict = None, timeout=(5, 20), stream=False):
    if params:
        req_url = url + "?" + urllib.parse.urlencode(params)
    else:
        req_url = url
        
    proxies = [
        "https://api.allorigins.win/raw?url=" + urllib.parse.quote(req_url),
        "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(req_url),
        "https://corsproxy.io/?" + urllib.parse.quote(req_url)
    ]
    
    last_error = None
    for proxy_url in proxies:
        try:
            response = requests.get(proxy_url, headers=headers, timeout=timeout, stream=stream)
            if response.status_code == 200:
                return response
        except requests.RequestException as error:
            print(f"프록시 접속 실패 ({proxy_url}): {error}")
            last_error = error
            continue
            
    if last_error:
        print("모든 프록시 서버 접속 실패, 다이렉트 연결 시도")
    
    return requests.get(req_url, headers=headers, timeout=timeout, stream=stream)

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
            detail="ALADIN_TTB_KEY 환경변수가 설정되지 않았습니다."
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
        response = safe_requests_get(url, params=request_params, headers=DEFAULT_HEADERS, timeout=(5, timeout))
    except requests.RequestException as error:
        raise HTTPException(status_code=502, detail={"message": "네트워크 오류", "reason": str(error)})

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail={"message": "요청 거부", "upstreamStatus": response.status_code})

    try:
        return response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail={"message": "JSON 파싱 오류", "upstreamStatus": response.status_code})

def normalize_cover_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    return url.replace("http://", "https://", 1).replace("coversum", "cover500").replace("cover200", "cover500").replace("/cover/", "/cover500/")

@app.get("/api/search")
def search_books(query: str = Query(..., min_length=1, max_length=200), max_results: int = Query(10, ge=1, le=50)):
    data = aladin_api_get("ItemSearch.aspx", {"Query": query.strip(), "QueryType": "Keyword", "MaxResults": max_results, "start": 1, "SearchTarget": "Book"})
    for book in data.get("item", []):
        if book.get("cover"):
            book["cover"] = normalize_cover_url(book["cover"])
    return data

@app.get("/api/ttb/search")
def ttb_search_proxy(Query_param: str = Query(..., alias="Query", min_length=1, max_length=200)):
    data = aladin_api_get("ItemSearch.aspx", {"Query": Query_param.strip(), "QueryType": "Keyword", "MaxResults": 10, "start": 1, "SearchTarget": "Book"})
    for book in data.get("item", []):
        if book.get("cover"):
            book["cover"] = normalize_cover_url(book["cover"])
    return data

def clean_text(value: str) -> str:
    if not value: return ""
    value = re.sub(r"\r", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    for word in ["접기", "펼쳐보기", "더보기", "책소개 전체", "공유하기", "보관함", "장바구니", "바로구매", "마이리스트"]:
        value = value.replace(word, "")
    return value.strip()

def extract_text_lines_from_soup(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "noscript", "iframe", "button"]):
        tag.decompose()
    lines = [clean_text(line) for line in soup.get_text("\n").split("\n")]
    return [line for line in lines if line and len(line) > 1]

def extract_section_by_heading(lines: list[str], start_headings: list[str], stop_headings: list[str]) -> str:
    start_index = -1
    for index, line in enumerate(lines):
        normalized = line.replace(" ", "")
        if any(h.replace(" ", "") in normalized for h in start_headings if len(normalized) <= 40):
            start_index = index
            break
    if start_index == -1: return ""
    
    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        normalized = lines[index].replace(" ", "")
        if any(s.replace(" ", "") in normalized for s in stop_headings if len(normalized) <= 40):
            end_index = index
            break
    return clean_text("\n".join(lines[start_index + 1:end_index]))

def split_phrase_list(text: str) -> list[str]:
    if not text: return []
    phrases = []
    for part in re.split(r"\n{2,}", text):
        part = clean_text(part)
        if not part: continue
        if len(part) > 600:
            phrases.extend([clean_text(item) for item in part.split("\n") if clean_text(item)])
        else:
            phrases.append(part)
            
    result, seen = [], set()
    for phrase in [p for p in phrases if len(p) >= 10]:
        key = phrase[:80]
        if key not in seen:
            seen.add(key)
            result.append(phrase)
    return result[:20]

def extract_by_original_boxes(soup: BeautifulSoup) -> dict:
    texts = {"story": "", "description": "", "phrases": [], "mdRecommend": ""}
    for box in soup.select(".Ere_prod_mconts_box"):
        title_el = box.select_one(".Ere_prod_mconts_LS")
        if not title_el: continue
        title = title_el.get_text(" ", strip=True)
        content_soup = BeautifulSoup(str(box.select_one(".Ere_prod_mconts_R") or box), "html.parser")
        
        for unwanted in content_soup(["script", "style", "noscript", "iframe", "button"]): unwanted.decompose()
        if content_soup.select_one(".Ere_prod_mconts_LS"): content_soup.select_one(".Ere_prod_mconts_LS").decompose()
            
        text_content = clean_text(content_soup.get_text("\n"))
        html_content = content_soup.decode_contents().strip()
        
        if "책소개" in title and "출판사" not in title: texts["story"] = text_content or html_content
        elif ("출판사" in title and ("책소개" in title or "상품소개" in title)) or "출판사 제공" in title: texts["description"] = text_content or html_content
        elif "책속에서" in title or "밑줄" in title: texts["phrases"] = split_phrase_list(text_content)
        elif "편집장의 선택" in title or "편집장" in title: texts["mdRecommend"] = text_content or html_content
    return texts

# 💡 하이브리드 ItemId 변환 (TTB API -> 스크래핑 우회)
def resolve_aladin_item_id(lookup_id: str, title: str = "", author: str = "", publisher: str = "") -> str:
    if not lookup_id: return ""
    raw = str(lookup_id).strip()
    digits = re.sub(r"[^0-9Xx]", "", raw)
    
    is_isbn13 = len(digits) == 13 and digits.startswith(("978", "979"))
    is_isbn10 = len(digits) == 10
    search_query = digits if (is_isbn13 or is_isbn10) else raw

    # 1. API 시도
    if is_isbn13 or is_isbn10:
        try:
            data = aladin_api_get("ItemLookUp.aspx", {"ItemId": digits, "ItemIdType": "ISBN13" if is_isbn13 else "ISBN"})
            items = data.get("item") or []
            if items and items[0].get("itemId"):
                return str(items[0]["itemId"])
        except Exception:
            pass

    # 2. API 실패 또는 카카오 UUID인 경우 스크래핑 우회
    if not search_query.isdigit() or len(search_query) < 10:
        if title or author:
            search_query = f"{title} {author} {publisher}".strip()
        else:
            return raw

    try:
        response = safe_requests_get(f"{ALADIN_WEB_BASE}/search/wsearchresult.aspx", params={"SearchTarget": "Book", "SearchWord": search_query}, headers=DEFAULT_HEADERS)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            first_link = soup.select_one("a.bo3")
            if first_link and first_link.get("href"):
                match = re.search(r"ItemId=(\d+)", first_link["href"], re.IGNORECASE)
                if match:
                    return str(match.group(1))
    except Exception as error:
        print(f"웹 검색 우회 실패: {error}")

    return raw

def scrape_aladin_texts(resolved_id: str) -> dict:
    url = f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_id}"
    texts = {"story": "", "description": "", "phrases": [], "mdRecommend": "", "sourceUrl": url}
    try:
        response = safe_requests_get(url, headers=DEFAULT_HEADERS, timeout=(5, 15))
        if response.status_code != 200: return texts
        soup = BeautifulSoup(response.text, "html.parser")
        boxed_texts = extract_by_original_boxes(soup)
        for key in ["story", "description", "mdRecommend", "phrases"]:
            if boxed_texts.get(key): texts[key] = boxed_texts[key]
            
        lines = extract_text_lines_from_soup(soup)
        stop_headings = ["책소개", "줄거리", "책속에서", "밑줄긋기", "편집장의 선택", "출판사 제공 책소개", "출판사 리뷰", "저자소개", "목차", "추천글", "상품정보", "회원리뷰"]
        if not texts["story"]: texts["story"] = extract_section_by_heading(lines, ["책소개", "줄거리"], stop_headings)
        if not texts["mdRecommend"]: texts["mdRecommend"] = extract_section_by_heading(lines, ["편집장의 선택"], stop_headings)
        if not texts["phrases"]: texts["phrases"] = split_phrase_list(extract_section_by_heading(lines, ["책속에서", "밑줄긋기"], stop_headings))
        if not texts["description"]: texts["description"] = extract_section_by_heading(lines, ["출판사 제공 상품소개", "출판사 제공 책소개", "출판사 리뷰"], stop_headings)
    except Exception:
        pass
    return texts

@app.get("/api/ttb/lookup")
def ttb_lookup_proxy(
    ItemId: str = Query(..., min_length=1),
    itemIdType: str = Query("ItemId"),
    OptResult: str = Query(""),
    title: str = Query(""),
    author: str = Query(""),
    publisher: str = Query("")
):
    try:
        data = aladin_api_get("ItemLookUp.aspx", {"ItemId": ItemId, "ItemIdType": itemIdType, "OptResult": OptResult})
        if data.get("item"): return data
    except Exception:
        pass

    resolved_id = ItemId
    if itemIdType.upper() in ["ISBN", "ISBN13"] or not ItemId.isdigit():
        resolved_id = resolve_aladin_item_id(ItemId, title, author, publisher)
        
    scraped = scrape_aladin_texts(resolved_id)
    item_page = 0
    try:
        response = safe_requests_get(f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_id}", headers=DEFAULT_HEADERS)
        if response.status_code == 200:
            page_match = re.search(r"(\d+)\s*쪽", response.text)
            if page_match: item_page = int(page_match.group(1))
    except Exception:
        pass

    return {
        "item": [{
            "itemId": resolved_id,
            "subInfo": {
                "itemPage": item_page,
                "story": scraped.get("story", ""),
                "fulldescription": scraped.get("description", ""),
                "fulldescription2": scraped.get("description", ""),
                "mdrecommend": scraped.get("mdRecommend", ""),
                "phraseList": [{"phrase": p} for p in scraped.get("phrases", [])]
            }
        }]
    }

def check_url(url: str) -> bool:
    try:
        return safe_requests_get(url, headers=DEFAULT_HEADERS, stream=True, timeout=(3, 5)).status_code == 200
    except requests.RequestException:
        return False

def normalize_aladin_image_url(src: Optional[str]) -> Optional[str]:
    if not src: return None
    src = src.strip().strip('"').strip("'")
    if src.startswith("//"): src = "https:" + src
    elif src.startswith("http://"): src = src.replace("http://", "https://", 1)
    if not src.startswith("https://image.aladin.co.kr/"): return None
    return src.replace("coversum", "cover500").replace("cover200", "cover500").replace("/cover/", "/cover500/")

def extract_image_from_node(node) -> Optional[str]:
    for attr in ["src", "data-src", "data-original", "data-lazy", "data-url"]:
        if node.get(attr): return normalize_aladin_image_url(node.get(attr))
    match = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", node.get("style", ""), re.IGNORECASE)
    if match: return normalize_aladin_image_url(match.group(1))
    return None

def extract_class_image(soup: BeautifulSoup, class_name: str) -> Optional[str]:
    div_node = soup.select_one(f".{class_name}")
    if not div_node:
        return None
    
    img = div_node.select_one("img")
    if img:
        # data-src, data-original 등 lazy-loading 속성도 봄
        url = extract_image_from_node(img)
        if url:
            return url
    
    # div 자체의 background-image 도 체크
    match = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", div_node.get("style", ""), re.IGNORECASE)
    if match:
        return normalize_aladin_image_url(match.group(1))
    
    # div 자체에 data-src 등이 붙어있는 경우도 확인
    for attr in ["data-src", "data-original", "data-url"]:
        if div_node.get(attr):
            return normalize_aladin_image_url(div_node.get(attr))
    
    return None


def extract_preview_page_images(item_id: str, headers: dict) -> dict:
    found = {"front": None, "spine": None, "back": None}
    try:
        response = safe_requests_get(f"{ALADIN_WEB_BASE}/shop/book/wletslookViewer.aspx?ItemId={item_id}", headers=headers)
        if response.status_code != 200: return found
        soup = BeautifulSoup(response.text, "html.parser")
        for key, selectors in {"front": [".pageType2.rightpage"], "spine": [".bookspine"], "back": [".pageType3.leftpage"]}.items():
            for sel in selectors:
                node = soup.select_one(sel)
                if node:
                    img_url = extract_image_from_node(node)
                    if img_url:
                        found[key] = img_url
                        break
    except Exception:
        pass
    return found

@app.get("/api/get-book-images")
def get_book_images(
    item_id: str = Query(..., min_length=1),
    title: str = Query(""),
    author: str = Query(""),
    publisher: str = Query("")
):
    resolved_item_id = resolve_aladin_item_id(item_id, title, author, publisher)
    url = f"{ALADIN_WEB_BASE}/shop/wproduct.aspx?ItemId={resolved_item_id}"
    images = {"front": None, "spine": None, "back": None, "resolvedItemId": resolved_item_id}
    
    try:
        response = safe_requests_get(url, headers=DEFAULT_HEADERS, timeout=(5, 15))
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(status_code=502, detail={"message": "페이지 호출 실패", "reason": str(error)})
        
    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    
    images["front"] = extract_class_image(soup, "c_front")
    images["spine"] = extract_class_image(soup, "c_left")
    images["back"] = extract_class_image(soup, "c_back")
    
    if not all([images["front"], images["spine"], images["back"]]):
        preview = extract_preview_page_images(resolved_item_id, DEFAULT_HEADERS)
        for key in ["front", "spine", "back"]:
            if not images[key] and preview.get(key): images[key] = preview[key]
            
    all_urls = re.findall(r'(?:https?:)?//image\.aladin\.co\.kr/product/[^"\'\s>)]+(?:\.jpg|\.png)', html, re.IGNORECASE)
    cover_url = next((normalize_aladin_image_url(s) for s in all_urls if "cover" in s.lower() and re.search(r"_\d\.", s)), None)
    
    if cover_url:
        if not images["front"]: images["front"] = cover_url
        match = re.search(r"(https?://image\.aladin\.co\.kr/product/\d+/\d+/)(?:[^/]+)/([^/]+?)_\d.*?\.(?:jpg|png)", cover_url, re.IGNORECASE)
        if match:
            spine_guess = f"{match.group(1)}spineflip/{match.group(2)}_d.jpg"
            back_guess = f"{match.group(1)}letslook/{match.group(2)}_b.jpg"
            if not images["spine"] and check_url(spine_guess): images["spine"] = spine_guess
            if not images["back"] and check_url(back_guess): images["back"] = back_guess
            
    for src in all_urls:
        n_src = normalize_aladin_image_url(src)
        if not n_src: continue
        if not images["back"] and re.search(r"(/letslook/|_(b|bl|wbl)\.jpg)$", n_src, re.IGNORECASE): images["back"] = n_src
        elif not images["spine"] and re.search(r"(/spine/|/spineflip/|_(d|s|sl)\.jpg)$", n_src, re.IGNORECASE): images["spine"] = n_src
        elif not images["front"] and re.search(r"(/cover500/|_(f|wfl|2)\.jpg)$", n_src, re.IGNORECASE): images["front"] = n_src
        
    return images
