"""
boatrace.jp スクレイパー

- 日次一覧 (race/index)        : ある日に開催される会場 + レース名 + 女子戦判定
- 出走表   (race/racelist)     : 選手・艇番・モーター等
- 結果     (race/raceresult)   : 着順・配当・決まり手

robots.txt は全開（User-agent: * / Disallow: 空）。
公式サイトに敬意を払うため request 間に 1 秒のスリープを挟む。
"""
from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_INTERVAL = 1.0  # 公式サイトへの負荷配慮

# ── 会場コード ──────────────────────────────────────────────────
VENUES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡", "08": "常滑",
    "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島",
    "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}

# ── 女子戦判定キーワード ─────────────────────────────────────────
LADIES_KEYWORDS = (
    "ヴィーナス", "オールレディース", "レディース",
    "クイーンズクライマックス", "クイーン", "女子", "GIIレディース",
)


@dataclass
class RaceEntry:
    """出走表 1艇分"""
    lane: int           # 艇番 (1-6)
    toban: str          # 登録番号
    name: str           # 選手名
    rank: str           # 級別 (A1/A2/B1/B2)
    branch: str         # 支部
    home: str           # 出身地（同表記の場合あり）
    age: Optional[int]  # 年齢
    weight: Optional[float]  # 体重
    flying: int         # フライング回数
    late: int           # 出遅れ回数
    avg_st: Optional[float]  # 平均ST
    national_win: Optional[float]   # 全国勝率
    national_2: Optional[float]     # 全国2連率
    national_3: Optional[float]     # 全国3連率
    local_win: Optional[float]      # 当地勝率
    local_2: Optional[float]        # 当地2連率
    local_3: Optional[float]        # 当地3連率
    motor_no: Optional[int]         # モーター番号
    motor_2: Optional[float]        # モーター2連率
    motor_3: Optional[float]        # モーター3連率
    boat_no: Optional[int]          # ボート番号
    boat_2: Optional[float]         # ボート2連率
    boat_3: Optional[float]         # ボート3連率


@dataclass
class RaceCard:
    """1レース分の出走表"""
    date: str           # YYYYMMDD
    jcd: str            # 会場コード
    venue: str          # 会場名
    rno: int            # レース番号
    title: str          # レースタイトル
    is_ladies: bool     # 女子戦か
    entries: list[RaceEntry] = field(default_factory=list)


@dataclass
class RaceResult:
    """1レース分の結果"""
    date: str
    jcd: str
    venue: str
    rno: int
    title: str
    finish: list[tuple[int, int, str, str]]  # (着順, 艇番, 登録番号, 名前)
    determinant: Optional[str]               # 決まり手
    payout_3t: Optional[str]                 # 3連単 組番
    payout_3t_yen: Optional[int]             # 3連単 配当
    payout_3f: Optional[str]                 # 3連複 組番
    payout_3f_yen: Optional[int]             # 3連複 配当
    wind_dir: Optional[str]
    wind_speed: Optional[int]
    wave: Optional[int]
    weather: Optional[str]


# ── HTTP セッション（スレッドローカル） ──────────────────────────
_tls = threading.local()
_last_request_at = [0.0]
_request_lock = threading.Lock()


def _session() -> requests.Session:
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.5"})
        _tls.s = s
    return _tls.s


def _get(url: str, *, timeout: float = 15.0) -> str:
    """レート制限つき GET。連続呼び出し時に REQUEST_INTERVAL 秒待つ。"""
    with _request_lock:
        elapsed = time.time() - _last_request_at[0]
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        _last_request_at[0] = time.time()
    r = _session().get(url, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = s.strip()
    if not s or s in {"-", "−", "ー"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None


# ── 日次一覧 ─────────────────────────────────────────────────────
def fetch_daily_venues(date: str) -> list[dict]:
    """
    指定日に開催されている全会場のリストを返す。

    Returns: [{ jcd, venue, title, period, is_ladies, ladies_marker }, ...]
    """
    url = f"{BASE}/owpc/pc/race/index?hd={date}"
    html = _get(url)
    soup = BeautifulSoup(html, "html.parser")

    venues_found: dict[str, dict] = {}

    # 各会場は <tr> 行で表現される。 raceindex リンクの jcd で識別
    for a in soup.select('a[href*="/owpc/pc/race/raceindex"]'):
        href = a.get("href", "")
        m = re.search(r"jcd=(\d{2})", href)
        if not m:
            continue
        jcd = m.group(1)
        if jcd in venues_found:
            continue
        title = a.get_text(" ", strip=True)

        tr = a.find_parent("tr")
        ladies_marker = False
        period = ""
        if tr:
            # is-venus / is-ladies など女子戦マーカーを検出
            for el in tr.find_all(class_=True):
                cls = el.get("class", [])
                if any(c in ("is-venus", "is-ladies", "is-allLadies") for c in cls):
                    ladies_marker = True
                    break
            # 開催期間 (例: "5/24-5/29 ３日目")
            period_td = tr.find("td", string=re.compile(r"\d+/\d+"))
            if period_td:
                period = period_td.get_text(" ", strip=True)

        is_ladies = ladies_marker or any(k in title for k in LADIES_KEYWORDS)

        venues_found[jcd] = {
            "jcd": jcd,
            "venue": VENUES.get(jcd, "?"),
            "title": title,
            "period": period,
            "is_ladies": is_ladies,
            "ladies_marker": ladies_marker,
        }

    return sorted(venues_found.values(), key=lambda v: v["jcd"])


# 後方互換のエイリアス
fetch_daily_index = fetch_daily_venues
fetch_daily_venues_simple = fetch_daily_venues


# ── 出走表 ───────────────────────────────────────────────────────
def fetch_racecard(date: str, jcd: str, rno: int) -> RaceCard:
    """指定レースの出走表を取得"""
    url = f"{BASE}/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={date}"
    html = _get(url)
    soup = BeautifulSoup(html, "html.parser")

    # タイトル
    title_el = soup.select_one("h2.heading2_titleName")
    title = title_el.get_text(strip=True) if title_el else ""

    is_ladies = any(k in title for k in LADIES_KEYWORDS)

    entries: list[RaceEntry] = []
    # 各艇は tbody.is-fs12 として表現される
    for tbody in soup.select("tbody.is-fs12"):
        first_tr = tbody.find("tr")
        if not first_tr:
            continue
        tds = first_tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue

        # 艇番（td[0]）
        lane_text = tds[0].get_text(strip=True)
        lane_match = re.search(r"\d", lane_text.translate(str.maketrans("１２３４５６", "123456")))
        if not lane_match:
            continue
        lane = int(lane_match.group())

        # 登録/級別/名前/支部/年齢/体重 は tds[2] のdivに入る
        info_td = tds[2]
        info_div_top = info_td.find("div", class_="is-fs11")
        # "4924 / B1"
        toban, rank = "", ""
        if info_div_top:
            tx = info_div_top.get_text(" ", strip=True)
            m = re.match(r"(\d+)\s*/\s*(\S+)", tx)
            if m:
                toban, rank = m.group(1), m.group(2)
        name_div = info_td.find("div", class_="is-fs18")
        name = name_div.get_text(strip=True) if name_div else ""
        # 支部/出身、年齢/体重
        bottom_divs = info_td.find_all("div", class_="is-fs11")
        branch = home = ""
        age = weight = None
        if len(bottom_divs) >= 2:
            tail = bottom_divs[-1].get_text(" ", strip=True)
            # 例: "長崎/愛知 33歳/48.2kg"
            m = re.match(r"([^/]+)/([^\s]+)\s+(\d+)歳/([\d.]+)kg", tail)
            if m:
                branch = m.group(1)
                home = m.group(2)
                age = int(m.group(3))
                weight = float(m.group(4))

        # td[3] = F0/L0/平均ST
        f_text = tds[3].get_text("|", strip=True).split("|")
        flying = _to_int(f_text[0]) or 0
        late = _to_int(f_text[1]) or 0
        avg_st = _to_float(f_text[2]) if len(f_text) > 2 else None

        # td[4] = 全国勝率/2連/3連
        nat = tds[4].get_text("|", strip=True).split("|")
        national_win = _to_float(nat[0]) if len(nat) > 0 else None
        national_2 = _to_float(nat[1]) if len(nat) > 1 else None
        national_3 = _to_float(nat[2]) if len(nat) > 2 else None

        # td[5] = 当地勝率/2連/3連
        loc = tds[5].get_text("|", strip=True).split("|")
        local_win = _to_float(loc[0]) if len(loc) > 0 else None
        local_2 = _to_float(loc[1]) if len(loc) > 1 else None
        local_3 = _to_float(loc[2]) if len(loc) > 2 else None

        # td[6] = モーター 番号/2連率/3連率
        mot = tds[6].get_text("|", strip=True).split("|")
        motor_no = _to_int(mot[0]) if len(mot) > 0 else None
        motor_2 = _to_float(mot[1]) if len(mot) > 1 else None
        motor_3 = _to_float(mot[2]) if len(mot) > 2 else None

        # td[7] = ボート 番号/2連率/3連率
        bot = tds[7].get_text("|", strip=True).split("|")
        boat_no = _to_int(bot[0]) if len(bot) > 0 else None
        boat_2 = _to_float(bot[1]) if len(bot) > 1 else None
        boat_3 = _to_float(bot[2]) if len(bot) > 2 else None

        entries.append(RaceEntry(
            lane=lane, toban=toban, name=name, rank=rank,
            branch=branch, home=home, age=age, weight=weight,
            flying=flying, late=late, avg_st=avg_st,
            national_win=national_win, national_2=national_2, national_3=national_3,
            local_win=local_win, local_2=local_2, local_3=local_3,
            motor_no=motor_no, motor_2=motor_2, motor_3=motor_3,
            boat_no=boat_no, boat_2=boat_2, boat_3=boat_3,
        ))

    return RaceCard(
        date=date, jcd=jcd, venue=VENUES.get(jcd, "?"),
        rno=rno, title=title, is_ladies=is_ladies, entries=entries,
    )


# ── 結果 ─────────────────────────────────────────────────────────
_KANJI_NUM = {"１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}


def fetch_result(date: str, jcd: str, rno: int) -> Optional[RaceResult]:
    """指定レースの結果を取得。未開催/未確定の場合 None。"""
    url = f"{BASE}/owpc/pc/race/raceresult?rno={rno}&jcd={jcd}&hd={date}"
    html = _get(url)
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h2.heading2_titleName")
    title = title_el.get_text(strip=True) if title_el else ""

    # 着順テーブル: 着 (1-6) + 艇番 + 登録番号 + 選手名 + タイム
    finish: list[tuple[int, int, str, str]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        # 1着〜6着が並ぶテーブルを判定
        ranks_seen = 0
        candidate: list[tuple[int, int, str, str]] = []
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            rank_txt = tds[0].get_text(strip=True)
            if rank_txt not in _KANJI_NUM:
                continue
            rank = _KANJI_NUM[rank_txt]
            lane_txt = tds[1].get_text(strip=True)
            try:
                lane = int(lane_txt)
            except ValueError:
                continue
            # tds[2] は "4414 大澤　　真菜" のような文字列
            name_txt = tds[2].get_text(" ", strip=True)
            m = re.match(r"(\d+)\s+(.+)", name_txt)
            toban = m.group(1) if m else ""
            name = (m.group(2) if m else name_txt).replace("　", "").strip()
            candidate.append((rank, lane, toban, name))
            ranks_seen += 1
        if ranks_seen >= 3:
            finish = candidate
            break

    if not finish:
        return None

    # 配当
    payout_3t = payout_3f = None
    payout_3t_yen = payout_3f_yen = None
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if "3連単" in txt and "3連複" in txt:
            # 順番に行を見て勝式判定
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                kind = tds[0].get_text(strip=True)
                combo = tds[1].get_text(" ", strip=True).replace(" ", "")
                yen_txt = tds[2].get_text(strip=True).replace(",", "").replace("¥", "")
                yen = _to_int(yen_txt)
                if kind == "3連単":
                    payout_3t = combo
                    payout_3t_yen = yen
                elif kind == "3連複":
                    payout_3f = combo
                    payout_3f_yen = yen
            break

    # 決まり手
    determinant = None
    full_text = soup.get_text(" ", strip=True)
    m = re.search(r"決まり手\s+(\S+)", full_text)
    if m:
        determinant = m.group(1)

    # 気象
    wind_dir = None
    wind_speed = None
    wave = None
    weather = None
    m = re.search(r"風速\s+(\d+)m", full_text)
    if m:
        wind_speed = int(m.group(1))
    m = re.search(r"波高\s+(\d+)cm", full_text)
    if m:
        wave = int(m.group(1))
    m = re.search(r"(晴|曇り?|雨|雪)\s", full_text)
    if m:
        weather = m.group(1)

    return RaceResult(
        date=date, jcd=jcd, venue=VENUES.get(jcd, "?"),
        rno=rno, title=title, finish=finish,
        determinant=determinant,
        payout_3t=payout_3t, payout_3t_yen=payout_3t_yen,
        payout_3f=payout_3f, payout_3f_yen=payout_3f_yen,
        wind_dir=wind_dir, wind_speed=wind_speed, wave=wave, weather=weather,
    )


# ── 動作確認 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1 and sys.argv[1] == "card":
        date, jcd, rno = sys.argv[2], sys.argv[3], int(sys.argv[4])
        c = fetch_racecard(date, jcd, rno)
        print(json.dumps(asdict(c), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "result":
        date, jcd, rno = sys.argv[2], sys.argv[3], int(sys.argv[4])
        r = fetch_result(date, jcd, rno)
        if r:
            print(json.dumps(asdict(r), ensure_ascii=False, indent=2))
        else:
            print("(no result yet)")
    elif len(sys.argv) > 1 and sys.argv[1] == "index":
        date = sys.argv[2]
        v = fetch_daily_venues_simple(date)
        print(json.dumps(v, ensure_ascii=False, indent=2))
    else:
        print("Usage: python3 scraper.py {card|result|index} YYYYMMDD [jcd] [rno]")
