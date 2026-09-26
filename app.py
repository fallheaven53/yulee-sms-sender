# -*- coding: utf-8 -*-
"""
율이공방 — 만족도 조사 문자 발송 웹앱
#2026-164W 경로 B: 현장 태블릿에서 번호 입력 → SMS_발송큐 시트 '대기' 행 기록
→ 사무실 PC 로컬 워커(sms_worker.py)가 폴링 → 슈어엠 API 발송.
(구 방식: Cloud Function 릴레이 호출. 7월 결제 축소로 릴레이 철수.)
"""

import re
import time
from datetime import datetime

import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

IS_ADMIN = st.query_params.get("admin", "") == "true"

st.set_page_config(
    page_title="2026 토요상설공연 만족도 조사",
    page_icon="📱",
    layout="centered",
    initial_sidebar_state="expanded" if IS_ADMIN else "collapsed",
)

if not IS_ADMIN:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] { display: none !important; }
        [data-testid="collapsedControl"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LOG_SHEET_NAME = "SMS_발송기록"
CONF_SHEET_NAME = "SMS_설정"
LOG_COLS = ["일시", "전화번호", "결과"]

QUEUE_SHEET_NAME = "SMS_발송큐"
QUEUE_COLS = ["요청ID", "요청시각", "전화번호", "링크", "상태", "완료시각", "결과"]

# #2026-164W 경로 B: 릴레이 CF 철수. 발송은 SMS_발송큐 시트 → 사무실 PC 로컬 워커.
# 아래 URL은 미사용(경로 A 릴레이 복귀 시 참조용으로만 남김).
RELAY_URL = "https://asia-northeast3-nice-abbey-473900-e6.cloudfunctions.net/sms-relay-surem"

# 본문 템플릿 — 만족도 조사만(BASE) / 만족도 + 유튜브 다시보기(FULL)
# 직전 단일 템플릿 원문(롤백 참조): "[광주문화재단] 토요상설공연 만족도 조사에 참여해 주세요.\n{link}"
MSG_TEMPLATE_BASE = (
    "[광주문화재단] 토요상설공연\n"
    "만족도 조사에 참여해 주세요.\n"
    "{form_url}"
)
MSG_TEMPLATE_FULL = (
    "[광주문화재단] 토요상설공연\n"
    "만족도 조사에 참여해 주세요.\n"
    "{form_url}\n"
    "\n"
    "공연 다시보기\n"
    "{youtube_url}"
)


def build_message(form_url, youtube_url=None):
    """발송 본문 조립. 유튜브 링크가 비어 있으면 BASE(만족도만)로 폴백."""
    yu = (youtube_url or "").strip()
    if yu:
        return MSG_TEMPLATE_FULL.format(form_url=form_url, youtube_url=yu)
    return MSG_TEMPLATE_BASE.format(form_url=form_url)


def clean_phone(phone):
    return re.sub(r"[^0-9]", "", str(phone or ""))

# ══════════════════════════════════════════════════════════════
#  구글 시트 연결 (발송기록 + 설정)
# ══════════════════════════════════════════════════════════════

@st.cache_resource
def get_sheet():
    if "gcp_service_account" not in st.secrets:
        return None
    sheet_id = st.secrets.get("satisfaction_sheet_id") or st.secrets.get("spreadsheet_id")
    if not sheet_id:
        return None
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


@st.cache_resource
def _ws_handles():
    """#2026-217W — 워크시트 핸들 캐시. sh.worksheet()는 매번 시트 메타데이터를 읽어
    분당 읽기 한도(워커와 같은 서비스 계정 공유)를 소모하므로 제목별로 한 번만 연다."""
    return {}


def _ws(sh, title, header):
    cache = _ws_handles()
    ws = cache.get(title)
    if ws is not None:
        return ws
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title, rows=1000, cols=max(5, len(header)))
        ws.update([header], value_input_option="RAW")
    cache[title] = ws
    return ws


def _is_quota_error(e):
    return isinstance(e, gspread.exceptions.APIError) and "429" in str(e)


class SheetBusy(Exception):
    """#2026-219W — 한도 초과 등으로 재시도 후에도 시트를 읽지 못함. 등록을 멈추고 «잠시 후 다시» 안내."""


def _read_with_retry(fn):
    """#2026-219W — 429면 2·4초 쉬고 다시(총 3회, enqueue_sms와 같은 간격). 끝내 실패하면 SheetBusy.
    get_sheet()의 open_by_key도 읽기라 fn 안에서 부르게 한다(cache_resource는 실패를 캐시하지 않음)."""
    for attempt in range(3):
        try:
            return fn()
        except Exception as e:
            if attempt < 2 and _is_quota_error(e):
                time.sleep(2 * (attempt + 1))
                continue
            raise SheetBusy(str(e)) from e


@st.cache_data(ttl=60, show_spinner=False)
def _conf_rows():
    """#2026-217W — 설정 시트 읽기 캐시(60초). 실패는 캐시하지 않고 예외로 올린다."""
    sh = get_sheet()
    if sh is None:
        return []
    return _ws(sh, CONF_SHEET_NAME, ["키", "값"]).get_all_values()


@st.cache_resource
def _conf_last_good():
    """#2026-219W — 마지막으로 읽은 설정 행(프로세스 공용). 캐시 만료 순간 429가 나도
    secrets 기본 링크로 나가지 않고 직전 시트 값을 쓴다."""
    return {}


def _conf_rows_safe():
    """#2026-219W — 설정 행: 캐시 → 재시도 → 직전 값. 셋 다 없으면 SheetBusy."""
    last = _conf_last_good()
    try:
        rows = _read_with_retry(_conf_rows)
    except SheetBusy:
        if "rows" in last:
            return last["rows"]
        raise
    last["rows"] = rows
    return rows


def _conf_value(key, default):
    """설정 시트 값 조회. 키가 없으면 default, 시트를 못 읽으면 SheetBusy(기본값으로 조용히 대체하지 않음)."""
    if get_sheet_safe() is None:
        return default
    for row in _conf_rows_safe()[1:]:
        if len(row) >= 2 and row[0] == key:
            return row[1]
    return default


def get_sheet_safe():
    """get_sheet()를 429 재시도로 감싼 것. 실패하면 SheetBusy."""
    return _read_with_retry(get_sheet)


@st.cache_data(ttl=30, show_spinner=False)
def _log_rows_for_status():
    """#2026-217W — 관리자 «오늘 발송 현황» 표시용 읽기 캐시(30초). 중복 확인에는 쓰지 않는다."""
    sh = get_sheet()
    if sh is None:
        return []
    return _ws(sh, LOG_SHEET_NAME, LOG_COLS).get_all_values()


def get_form_url():
    """설정 시트에서 구글폼 링크 조회 (없으면 secrets 기본값).
    내부 키 이름은 호환성 위해 naver_form_url을 그대로 사용하나, 저장되는 값은 실제 구글폼 URL이다.
    #2026-219W: 시트를 못 읽으면 기본값으로 대체하지 않고 SheetBusy를 올린다."""
    default = st.secrets.get("naver_form_url", "")
    return _conf_value("naver_form_url", "") or default


def set_form_url(url):
    sh = get_sheet()
    if sh is None:
        return False
    try:
        ws = _ws(sh, CONF_SHEET_NAME, ["키", "값"])
        rows = ws.get_all_values()
        target = None
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == "naver_form_url":
                target = i
                break
        if target:
            ws.update(f"B{target}", [[url]], value_input_option="RAW")
        else:
            ws.append_row(["naver_form_url", url], value_input_option="RAW")
        _conf_rows.clear()
        return True
    except Exception as e:
        st.sidebar.error(f"설정 저장 실패: {e}")
        return False


YOUTUBE_KEY = "youtube_replay_url"


def get_youtube_url():
    """설정 시트에서 회차 유튜브 다시보기 링크 조회 (없으면 secrets 기본값 또는 빈 문자열).
    #2026-219W: 시트를 못 읽으면 기본값으로 대체하지 않고 SheetBusy를 올린다."""
    default = st.secrets.get("youtube_replay_url_default", "")
    return _conf_value(YOUTUBE_KEY, default)


def set_youtube_url(url):
    """유튜브 다시보기 링크 저장 (빈 문자열 저장 시 폴백 모드)."""
    sh = get_sheet()
    if sh is None:
        return False
    try:
        ws = _ws(sh, CONF_SHEET_NAME, ["키", "값"])
        rows = ws.get_all_values()
        target = None
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == YOUTUBE_KEY:
                target = i
                break
        if target:
            ws.update(f"B{target}", [[url]], value_input_option="RAW")
        else:
            ws.append_row([YOUTUBE_KEY, url], value_input_option="RAW")
        _conf_rows.clear()
        return True
    except Exception as e:
        st.sidebar.error(f"유튜브 링크 저장 실패: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  SMS 발송 (#2026-164W 경로 B — SMS_발송큐 시트 + 사무실 PC 로컬 워커)
# ══════════════════════════════════════════════════════════════

def enqueue_sms(phone, payload):
    """SMS_발송큐 시트에 '대기' 행 추가 → 요청ID 반환(실패 시 None).
    payload = 실제 발송 본문 전체(워커가 verbatim 발송). '링크' 열에 저장."""
    sh = get_sheet()
    if sh is None:
        return None
    try:
        req_id = f"{int(time.time() * 1000)}_{clean_phone(phone)[-4:]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # #2026-217W — 한도 초과(429)면 짧게 기다렸다 다시(총 3회). 그 밖의 오류는 즉시 실패.
        for attempt in range(3):
            try:
                ws = _ws(sh, QUEUE_SHEET_NAME, QUEUE_COLS)
                ws.append_row(
                    [req_id, now, clean_phone(phone), payload, "대기", "", ""],
                    value_input_option="RAW",
                )
                return req_id
            except Exception as e:
                if attempt < 2 and _is_quota_error(e):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        st.warning(f"큐 저장 실패: {e}")
        return None


def send_sms(phone, form_url, youtube_url=None):
    """#2026-164W: 릴레이 CF 호출 대신 SMS_발송큐 시트에 발송 요청을 기록한다.
    사무실 PC의 sms_worker.py가 폴링해 슈어엠 API로 실제 발송한다.
    반환 시그니처(ok, msg)는 릴레이 방식과 동일하게 유지."""
    text = build_message(form_url, youtube_url)
    req_id = enqueue_sms(phone, text)
    if not req_id:
        return False, "발송 요청 저장 실패 (시트 큐)"
    return True, "발송 요청 접수"


def log_to_sheet(phone, result):
    sh = get_sheet()
    if sh is None:
        return
    try:
        ws = _ws(sh, LOG_SHEET_NAME, LOG_COLS)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([now, phone, result], value_input_option="RAW")
    except Exception:
        pass


def is_duplicate_today(phone):
    """#2026-219W: 429 등으로 발송기록을 못 읽으면 «중복 아님»으로 통과시키지 않고 SheetBusy를 올린다.
    (예전엔 except: pass로 False를 돌려 같은 번호가 두 번 발송될 수 있었다 — 218W 2절 ①)"""
    sh = get_sheet_safe()
    if sh is None:
        return False
    rows = _read_with_retry(lambda: _ws(sh, LOG_SHEET_NAME, LOG_COLS).get_all_values())
    today = datetime.now().strftime("%Y-%m-%d")
    for row in rows[1:]:
        if (len(row) >= 3
                and row[0].startswith(today)
                and clean_phone(row[1]) == clean_phone(phone)
                and "성공" in row[2]):
            return True
    return False


def _process_registration(raw):
    """#2026-112W 완성 번호 등록 처리(검증·중복·발송·기록). status만 세팅, rerun은 호출부.
    구 form(?phone=)·키오스크 버튼 그리드 공용 진입점.
    #2026-164W: send_sms는 큐 기록만 하고 실제 발송·발송기록은 워커가 담당.
    큐 등록 실패 시에만 여기서 log_to_sheet로 남긴다."""
    clean = clean_phone(raw)
    if len(clean) < 10 or not clean.startswith("01"):
        st.session_state["status"] = "error"
        st.session_state["status_msg"] = "올바른 휴대폰 번호를 입력해주세요"
        st.session_state["status_time"] = time.time()
        return
    # #2026-219W: 중복 확인·설정 조회를 시트 한도 때문에 못 하면 큐에 넣지 않고 다시 누르게 한다.
    # 입력한 번호는 키패드에 되살려 두어 «확인»만 다시 누르면 된다.
    try:
        is_dup = is_duplicate_today(clean)
        if not is_dup:
            form_url = get_form_url()
            youtube_url = get_youtube_url()
    except SheetBusy:
        st.session_state["status"] = "retry"
        st.session_state["status_msg"] = "잠시 후 다시 시도해 주세요"
        st.session_state["kiosk_phone"] = clean
        st.session_state["status_time"] = time.time()
        return
    if is_dup:
        st.session_state["status"] = "dup"
        st.session_state["status_msg"] = "이미 발송된 번호입니다"
    else:
        if not form_url:
            st.session_state["status"] = "error"
            st.session_state["status_msg"] = "설문 링크가 설정되지 않았습니다"
        else:
            ok, result = send_sms(clean, form_url, youtube_url)
            if not ok:
                log_to_sheet(clean, result)
            if ok:
                st.session_state["status"] = "success"
                st.session_state["status_msg"] = "문자가 발송되었습니다. 감사합니다!"
            else:
                st.session_state["status"] = "error"
                st.session_state["status_msg"] = f"발송 실패: {result}"
    st.session_state["status_time"] = time.time()


# ══════════════════════════════════════════════════════════════
#  스타일 (태블릿 큰 UI)
# ══════════════════════════════════════════════════════════════

st.markdown("""
<style>
html, body, [class*="css"] { font-size: 22px !important; }
h1 { font-size: 42px !important; text-align: center; padding-top: 10px; }
.subtitle { font-size: 26px !important; text-align: center; color: #CCCCCC; margin-bottom: 30px; }
.notice { font-size: 24px !important; text-align: center; color: #F5C542; margin: 20px 0; }
input[type="tel"], input[type="text"] {
    font-size: 36px !important; text-align: center; height: 72px !important;
    letter-spacing: 3px;
}
div.stButton > button {
    font-size: 40px !important; height: 140px; width: 100%;
    background-color: #F5C542; color: #111; font-weight: 700;
    border-radius: 16px; border: none;
}
div.stButton > button:hover { background-color: #FFD75E; }
.footer { font-size: 18px !important; text-align: center; color: #888; margin-top: 40px; }
.success-box {
    font-size: 32px !important; text-align: center;
    background: #1b5e20; color: #fff; padding: 40px; border-radius: 16px;
    margin: 30px 0;
}
.error-box {
    font-size: 28px !important; text-align: center;
    background: #b71c1c; color: #fff; padding: 30px; border-radius: 16px;
    margin: 30px 0;
}
.warn-box {
    font-size: 28px !important; text-align: center;
    background: #e65100; color: #fff; padding: 30px; border-radius: 16px;
    margin: 30px 0;
}
/* #2026-112W 키오스크 / #2026-114W 감성 / #2026-116W 반응형 대응 */
/* ② 상단 Fork·GitHub·⋮ 메뉴 숨김(관객용) */
[data-testid="stToolbar"], [data-testid="stHeader"], #MainMenu, header { display: none !important; visibility: hidden !important; }
/* 여백 축소 + #2026-116W 데스크톱 과대 방지(중앙 카드 최대폭) */
.block-container { padding-top: 1.2rem !important; padding-bottom: 0.5rem !important;
                   max-width: 700px !important; }
h1 { padding-top: 2px !important; margin-bottom: 4px !important;
     font-size: clamp(20px, 6vw, 44px) !important; white-space: nowrap !important; }
.subtitle { margin-bottom: 12px !important; font-size: clamp(15px, 4vw, 26px) !important; }
/* #2026-116W 좁은 폭에서 3열 유지(st.columns 세로 스택 방지 — 430px 스택 지점) */
[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: clamp(6px, 1.5vw, 12px) !important; }
[data-testid="stColumn"] { min-width: 0 !important; flex: 1 1 0 !important; width: auto !important; }
/* ① 표시줄 세로 중앙 + #2026-116W 크기 뷰포트 비례(clamp) */
.kiosk-disp { font-size: clamp(28px, 8vw, 46px); text-align: center; letter-spacing: 2px;
              height: clamp(56px, 13vw, 88px);
              display: flex; align-items: center; justify-content: center;
              border-radius: 14px; border: 2px solid #555;
              background: #1e1e1e; color: #fff; margin: 8px 0 4px; overflow: hidden; }
.kiosk-disp.ph { color: #666; }
.kiosk-hint { text-align: center; color: #E74C3C; font-size: clamp(15px, 4vw, 22px); min-height: 26px; margin-bottom: 4px; }
/* #2026-116W 키패드·확인 크기 비례(좁으면 알맞게·넓으면 시원하게·상한으로 과대 방지) */
div.stButton > button[kind="secondary"] { background: #2b2b2b !important; color: #fff !important;
              height: clamp(48px, 12vw, 88px) !important; font-size: clamp(20px, 5.5vw, 34px) !important;
              padding: 0 !important; }
div.stButton > button[kind="secondary"]:hover { background: #404040 !important; }
div.stButton > button[kind="primary"] { background: #F5C542 !important; color: #111 !important;
              height: clamp(56px, 14vw, 96px) !important; font-size: clamp(22px, 6vw, 36px) !important; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  사이드바 — 관리자 페이지
# ══════════════════════════════════════════════════════════════

if IS_ADMIN:
    with st.sidebar:
        st.header("🔧 관리자")
        admin_pw = st.secrets.get("admin_password", "")
        pw_in = st.text_input("비밀번호", type="password")
        if admin_pw and pw_in == admin_pw:
            st.success("관리자 인증")
            # #2026-219W: 시트를 못 읽으면 입력칸에 기본 링크를 채워 넣지 않는다(저장 시 덮어쓰기 방지).
            try:
                cur_url = get_form_url()
                cur_yt = get_youtube_url()
                _conf_ok = True
            except SheetBusy:
                cur_url, cur_yt, _conf_ok = "", "", False
                st.warning("설정 시트를 지금 읽지 못했습니다 — 잠시 후 새로고침해 주세요. 이 상태에서는 저장하지 마세요.")
            new_url = st.text_input("구글폼 링크", value=cur_url, key="form_url_input")
            if st.button("구글폼 링크 저장", disabled=not _conf_ok):
                if set_form_url(new_url):
                    st.success("저장 완료")
                    st.rerun()

            st.divider()
            st.caption("🎬 회차 유튜브 다시보기 링크")
            new_yt = st.text_input(
                "유튜브 다시보기 링크",
                value=cur_yt,
                key="youtube_url_input",
                placeholder="이 회차 유튜브 라이브 URL (비워두면 만족도 조사만 발송)",
            )
            yt_c1, yt_c2 = st.columns(2)
            with yt_c1:
                if st.button("유튜브 링크 저장", disabled=not _conf_ok):
                    if set_youtube_url(new_yt.strip()):
                        st.success("저장 완료")
                        st.rerun()
            with yt_c2:
                if st.button("유튜브 링크 비우기", disabled=not _conf_ok):
                    if set_youtube_url(""):
                        st.success("비움 (폴백 모드)")
                        st.rerun()

            # 본문 미리보기 + EUC-KR 바이트 / SMS·LMS 분기 표시
            preview = build_message(new_url, new_yt)
            st.caption("실제 발송 본문 미리보기:")
            st.code(preview, language="text")
            byte_len = len(preview.encode("euc-kr", errors="replace"))
            msg_type = "LMS" if byte_len > 90 else "SMS"
            st.caption(f"본문 길이: {byte_len} 바이트 / 분기: {msg_type} (90바이트 초과 시 LMS)")

            st.divider()
            if st.button("🔄 캐시 초기화"):
                get_sheet.clear()
                _ws_handles.clear()
                _conf_rows.clear()
                _log_rows_for_status.clear()
                st.rerun()

            st.divider()
            st.caption("📋 오늘 발송 현황")
            sh = get_sheet()
            if sh is not None:
                try:
                    rows = _log_rows_for_status()
                    today = datetime.now().strftime("%Y-%m-%d")
                    todays = [r for r in rows[1:] if r and len(r) >= 3 and r[0].startswith(today)]
                    ok_cnt = len([r for r in todays if "성공" in r[2]])
                    fail_cnt = len([r for r in todays if "성공" not in r[2]])
                    m1, m2 = st.columns(2)
                    m1.metric("성공", ok_cnt)
                    m2.metric("실패", fail_cnt)
                    if todays:
                        import pandas as pd
                        df = pd.DataFrame(todays[-10:], columns=LOG_COLS[:len(todays[-1])])
                        st.caption("최근 10건")
                        st.dataframe(df, use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(f"기록 조회 실패: {e}")
        elif pw_in:
            st.error("비밀번호가 틀렸습니다.")


# ══════════════════════════════════════════════════════════════
#  메인 화면
# ══════════════════════════════════════════════════════════════

st.markdown("<h1>📋 토요상설공연 관람 등록</h1>", unsafe_allow_html=True)
st.markdown(
    "<div class='subtitle'>전화번호를 입력하시면 설문 링크와<br>유튜브 링크를 문자로 보내드립니다</div>",
    unsafe_allow_html=True,
)

# 상태 관리
if "status" not in st.session_state:
    st.session_state["status"] = None  # None / "success" / "error" / "dup" / "retry"(#2026-219W)
    st.session_state["status_time"] = 0
    st.session_state["status_msg"] = ""

status = st.session_state["status"]

# 자동 리셋 (3초 후)
if status is not None and time.time() - st.session_state["status_time"] >= 3:
    st.session_state["status"] = None
    st.session_state["status_msg"] = ""
    st.rerun()

if status == "success":
    st.markdown(f"<div class='success-box'>✅ {st.session_state['status_msg']}</div>",
                unsafe_allow_html=True)
    time.sleep(1)
    st.rerun()
elif status in ("dup", "retry"):
    st.markdown(f"<div class='warn-box'>⚠ {st.session_state['status_msg']}</div>",
                unsafe_allow_html=True)
    time.sleep(1)
    st.rerun()
elif status == "error":
    st.markdown(f"<div class='error-box'>❌ {st.session_state['status_msg']}</div>",
                unsafe_allow_html=True)
    time.sleep(1)
    st.rerun()
else:
    # #2026-112W 키오스크 버튼 그리드 (안 a) — 값 전달을 st.session_state로 처리한다.
    # (components.v1.html top-nav는 iframe sandbox에 allow-top-navigation이 없어 차단됨.)
    # 키 클릭은 on_click 콜백으로 번호를 갱신, 확인 시 _process_registration 호출. 저장·발송 무변경.
    _KP = "kiosk_phone"
    if _KP not in st.session_state:
        st.session_state[_KP] = ""
    if "kiosk_hint" not in st.session_state:
        st.session_state["kiosk_hint"] = ""

    def _kp_press(k):
        p = st.session_state[_KP]
        if k == "del":
            st.session_state[_KP] = p[:-1]
        elif k == "010":
            if p == "":
                st.session_state[_KP] = "010"
        elif len(p) < 11:
            st.session_state[_KP] = p + k
        st.session_state["kiosk_hint"] = ""

    def _kp_fmt(n):
        if not n:
            return "010-0000-0000"
        if len(n) <= 3:
            return n
        if len(n) <= 7:
            return n[:3] + "-" + n[3:]
        return n[:3] + "-" + n[3:7] + "-" + n[7:]

    _cur = st.session_state[_KP]
    st.markdown(
        f"<div class='kiosk-disp{' ph' if not _cur else ''}'>{_kp_fmt(_cur)}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='kiosk-hint'>{st.session_state['kiosk_hint']}</div>",
        unsafe_allow_html=True,
    )
    for _row in (["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"], ["010", "0", "del"]):
        _cols = st.columns(3)
        for _i, _k in enumerate(_row):
            _label = {"del": "←", "010": "010"}.get(_k, _k)
            _cols[_i].button(_label, key=f"kp_{_k}", type="secondary",
                             use_container_width=True, on_click=_kp_press, args=(_k,))
    if st.button("확인", key="kp_confirm", type="primary", use_container_width=True):
        _p = st.session_state[_KP]
        if len(_p) < 10 or not _p.startswith("010"):
            st.session_state["kiosk_hint"] = "번호를 확인해 주세요"
            st.rerun()
        else:
            st.session_state[_KP] = ""
            _process_registration(_p)
            st.rerun()

    # (구 form 하위호환) 쿼리파라미터 phone 처리 — 공용 진입점 재사용
    _q = st.query_params.get("phone", "")
    if _q:
        try:
            del st.query_params["phone"]
        except KeyError:
            pass
        _process_registration(_q)
        st.rerun()

st.markdown(
    "<div class='footer'>입력하신 번호는 만족도 조사 링크 발송에만 사용됩니다.<br>"
    "발송 기록은 해당 공연일 기준으로만 보관됩니다.</div>",
    unsafe_allow_html=True,
)
