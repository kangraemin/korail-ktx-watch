"""KTX 빈자리(취소표) 감시 + 자동 예약.

지정한 구간·날짜·시간대의 KTX를 몇 초 간격으로 조회하다가 빈자리가 나면 바로 예약하고 알린다.
기본은 예약만 잡고 결제는 코레일톡 앱에서 직접 한다(결제 기한 안에 안 하면 자동 취소).
--auto-pay를 주면 .env의 카드로 바로 결제까지 한다.

예:
  python ktx_watch.py --dep 부산 --arr 서울,수서 --date 20261020 --from 1700 --to 2100
  python ktx_watch.py --dep 서울 --arr 부산 --date 20261020 --from 0800 --to 1000 --adults 2 --no-special
자세한 옵션: python ktx_watch.py -h
"""
import argparse
import os
import platform
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from ktx import (
    AdultPassenger,
    Korail,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    SoldOutError,
    TrainType,
)

SEAT_DIRS = {"any": "000", "forward": "009", "backward": "010"}


def parse_args():
    p = argparse.ArgumentParser(description="KTX 빈자리 감시 + 자동 예약")
    p.add_argument("--dep", required=True, help="출발역 (예: 서울, 부산, 수서)")
    p.add_argument("--arr", required=True, help="도착역. 쉼표로 여러 개 (예: 서울,수서)")
    p.add_argument("--date", required=True, help="출발 날짜 YYYYMMDD (예: 20261020)")
    p.add_argument("--from", dest="time_from", default="0000", help="이 시각 이후 출발 HHMM (기본 0000)")
    p.add_argument("--to", dest="time_to", default="2359", help="이 시각 이전 출발 HHMM, 포함 (기본 2359)")
    p.add_argument("--arrive-before", default="", help="이 시각 전에 도착하는 열차만 HHMM (기본 제한 없음)")
    p.add_argument("--adults", type=int, default=1, help="어른 인원 (기본 1)")
    p.add_argument("--seat-dir", choices=SEAT_DIRS, default="any", help="좌석 방향 any/forward(순방향)/backward(역방향)")
    p.add_argument("--no-special", action="store_true", help="일반실만 (기본은 일반실 우선, 없으면 특실)")
    p.add_argument("--interval", type=float, nargs=2, default=(3.0, 5.0), metavar=("MIN", "MAX"),
                   help="조회 간격 초 (기본 3~5초 무작위). 너무 짧으면 차단될 수 있음")
    p.add_argument("--auto-pay", action="store_true",
                   help="예약되면 .env의 카드로 바로 결제 (기본은 예약만 하고 결제는 코레일톡에서)")
    a = p.parse_args()
    a.arrs = [s.strip() for s in a.arr.split(",") if s.strip()]
    a.time_from = a.time_from.ljust(6, "0")
    a.time_to = a.time_to.ljust(6, "0")
    a.arrive_before = a.arrive_before.ljust(6, "0") if a.arrive_before else "999999"
    return a


ARGS = None


def time_from():
    """당일이면 지금+15분 뒤 출발부터(매 회 갱신), 아니면 --from 값."""
    if ARGS.date == datetime.now().strftime("%Y%m%d"):
        return max(ARGS.time_from, (datetime.now() + timedelta(minutes=15)).strftime("%H%M00"))
    return ARGS.time_from


def notify(title, msg):
    """터미널 출력 + (설정 시) 텔레그램 + (맥이면) 알림·음성."""
    print(f"\n*** {title}: {msg}\n", flush=True)
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": f"🚄 {title}\n{msg}"},
                timeout=10,
            )
        except requests.RequestException as e:
            print(f"텔레그램 실패: {e}")
    if platform.system() == "Darwin":
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Glass"'],
            check=False,
        )
        subprocess.run(["say", title], check=False)


def candidates(korail, start_from):
    # 한 번 조회에 최대 ~10편만 오므로 마지막 열차 다음 시각부터 이어서 조회
    for arr in ARGS.arrs:
        start, seen = start_from, set()
        while start <= ARGS.time_to:
            try:
                trains = korail.search_train(
                    ARGS.dep, arr, ARGS.date, start, train_type=TrainType.KTX,
                    passengers=[AdultPassenger(ARGS.adults)], include_no_seats=True,
                )
            except NoResultsError:
                break
            new = [t for t in trains if t.train_no not in seen and t.dep_date == ARGS.date]
            if not new:
                break
            for t in new:
                seen.add(t.train_no)
                if start_from <= t.dep_time <= ARGS.time_to and t.arr_time < ARGS.arrive_before:
                    yield t
            last = max(t.dep_time for t in new)
            if last >= ARGS.time_to:
                break
            start = f"{int(last[:4]) + 1:04d}00" if last[2:4] != "59" else f"{int(last[:2]) + 1:02d}0000"


def in_window(t):
    return t.dep_date == ARGS.date and ARGS.time_from <= t.dep_time <= ARGS.time_to


def held_reservation(korail):
    """대상 시간대의 미결제 예약이 있으면 반환."""
    try:
        rsvs = korail.reservations()
    except NoResultsError:
        return None
    return next((r for r in (rsvs if isinstance(rsvs, list) else [rsvs]) if in_window(r)), None)


def paid_ticket(korail):
    try:
        return next((t for t in korail.tickets() if in_window(t)), None)
    except NoResultsError:
        return None


def load_card():
    """--auto-pay용 카드 정보를 .env에서 읽고 형식을 검사한다. 값은 절대 출력하지 않는다."""
    card = {
        "number": re.sub(r"\D", "", os.getenv("CARD_NUMBER", "")),
        "pw2": os.getenv("CARD_PW2", "").strip(),
        "expire": re.sub(r"\D", "", os.getenv("CARD_EXPIRE", "")),
        "birth": re.sub(r"\D", "", os.getenv("CARD_BIRTH", "")),
        "installment": os.getenv("CARD_INSTALLMENT", "0").strip() or "0",
    }
    problems = []
    if not 14 <= len(card["number"]) <= 16:
        problems.append("CARD_NUMBER: 카드번호 14~16자리")
    if not re.fullmatch(r"\d{2}", card["pw2"]):
        problems.append("CARD_PW2: 카드 비밀번호 앞 2자리")
    if not re.fullmatch(r"\d{2}(0[1-9]|1[0-2])", card["expire"]):
        problems.append("CARD_EXPIRE: 유효기간 YYMM (예: 2908 = 2029년 8월)")
    if len(card["birth"]) not in (6, 10):
        problems.append("CARD_BIRTH: 개인카드는 생년월일 YYMMDD, 법인카드는 사업자등록번호 10자리")
    if not card["installment"].isdigit():
        problems.append("CARD_INSTALLMENT: 할부 개월 수 숫자 (0 = 일시불)")
    if problems:
        sys.exit("--auto-pay에 필요한 .env 카드 정보가 비었거나 형식이 달라요:\n  " + "\n  ".join(problems))
    # 생년월일 6자리 = 개인(J), 사업자번호 10자리 = 법인(S)
    card["type"] = "J" if len(card["birth"]) == 6 else "S"
    return card


def pay(korail, rsv, card):
    """카드 결제. 성공하면 True, 실패하면 알리고 False."""
    try:
        if korail.pay_with_card(rsv, card["number"], card["pw2"], card["birth"], card["expire"],
                                int(card["installment"]), card["type"]):
            notify("KTX 결제 완료", f"{rsv.dep_name}→{rsv.arr_name} {rsv.price:,}원 결제·발권 완료. 감시 종료")
            return True
        reason = "결제 응답 실패"
    except KorailError as e:
        reason = str(e)
    notify("KTX 자동 결제 실패", f"{reason} — 예약은 잡혀 있으니 코레일톡에서 직접 결제하세요")
    return False


def wait_for_payment(korail, rsv):
    """결제되면 True, 결제 기한이 지나 취소되면 False."""
    while True:
        time.sleep(30)
        try:
            if held_reservation(korail):
                continue
            if paid_ticket(korail):
                notify("KTX 결제 확인", f"{rsv.dep_name}→{rsv.arr_name} 발권 완료. 감시 종료")
                return True
            notify("KTX 예약 취소됨", "결제 기한이 지나 취소됨 → 다시 빈자리 감시")
            return False
        except NeedToLoginError:
            korail.login()
        except (KorailError, ValueError, OSError) as e:
            print(f"\n결제 확인 오류: {e}")


def main():
    global ARGS
    ARGS = parse_args()
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    kid, kpw = os.getenv("KORAIL_ID"), os.getenv("KORAIL_PW")
    if not kid or not kpw:
        sys.exit(".env에 KORAIL_ID / KORAIL_PW를 채워 주세요 (.env.example 참고).")
    card = load_card() if ARGS.auto_pay else None

    korail = Korail(kid, kpw, auto_login=False)
    if not korail.login():
        sys.exit("로그인 실패: 아이디/비밀번호를 확인하세요.")
    print(f"로그인 성공. {ARGS.dep}→{','.join(ARGS.arrs)} {ARGS.date} "
          f"{ARGS.time_from[:4]}~{ARGS.time_to[:4]} 출발, 어른 {ARGS.adults}명 감시 시작"
          f"{' · 자동 결제 켜짐' if card else ''} (Ctrl+C로 종료)")

    if paid_ticket(korail):
        sys.exit("이미 대상 시간대 승차권이 발권돼 있음 → 종료")
    held = held_reservation(korail)
    if held:
        print(f"기존 미결제 예약 발견: {held}")
        if card and pay(korail, held, card):
            return
        if wait_for_payment(korail, held):
            return

    option = ReserveOption.GENERAL_ONLY if ARGS.no_special else ReserveOption.GENERAL_FIRST
    attempt = 0
    while True:
        attempt += 1
        start_from = time_from()
        if ARGS.date < datetime.now().strftime("%Y%m%d") or start_from > ARGS.time_to:
            notify("KTX 감시 종료", "감시 시간대가 지났어요 — 빈자리를 못 잡았어요")
            return
        try:
            trains = list(candidates(korail, start_from))
            if attempt == 1:
                print(f"감시 대상 {len(trains)}편:")
                for t in trains:
                    print(" ", t)
            for t in trains:
                if not (t.has_general_seat() if ARGS.no_special else t.has_seat()):
                    continue
                try:
                    rsv = korail.reserve(t, [AdultPassenger(ARGS.adults)], option=option,
                                         seat_dir=SEAT_DIRS[ARGS.seat_dir])
                except SoldOutError:
                    print(f"{datetime.now():%H:%M:%S} 예약 직전 매진: {t}")
                    continue
                print(f"\n{rsv}")
                if card:
                    notify("KTX 예약 성공", f"{rsv} — 자동 결제 시도")
                    if pay(korail, rsv, card):
                        return
                else:
                    notify("KTX 예약 성공", f"{rsv} — 코레일톡에서 결제하세요")
                if wait_for_payment(korail, rsv):
                    return
                break
            print(f"\r{datetime.now():%H:%M:%S} #{attempt} 빈자리 없음", end="", flush=True)
        except NeedToLoginError:
            print("\n세션 만료 → 재로그인")
            korail.login()
        except (KorailError, ValueError, OSError) as e:
            print(f"\n{datetime.now():%H:%M:%S} 오류: {e} — 30초 대기")
            time.sleep(30)
        time.sleep(random.uniform(*ARGS.interval))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단했어요.")
