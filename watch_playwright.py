#!/usr/bin/env python3
"""DP «Документ» Мюнхен — монитор слотов через реальный Chrome на GitHub Actions.

Почему так: Cloudflare режет наш домашний IP и не пропускает POST, а у раннера
GitHub IP чистый. POST делает не скрипт, а сама страница (Chrome), поэтому знать
тело запроса не нужно: мы перехватываем ответ приложения `{"days": [...]}`.

Логика:
  1) Chrome открывает страницу очереди (ждём, пока пройдёт челлендж Cloudflare).
  2) Перехватываем POST-ответ на /solutions/e-queue со стороны страницы.
  3) Выбираем услугу service=4 (значение «Закордонний паспорт та (або) ID-картка»).
  4) Ждём JSON: [] → мест нет; [{date, allowedJobCount}] → места есть → Telegram.

Секреты репозитория: TG_DELIVERY (токен бота), TG_TARGET (id чата).
Ничего не бронирует и персональные данные не отправляет.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PAGE = "https://munich.pasport.org.ua/solutions/e-queue"
SERVICE_VALUE = "4"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "1"))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
CHALLENGE_TITLE = re.compile(r"just a moment|зачекайте|перевірка безпеки", re.I)


def env_str(name, default=""):
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def stamp():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=1))).strftime("%d.%m.%Y %H:%M")


def normalize_date(value):
    s = str(value or "").strip()
    m = re.match(r"^(20\d{2})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m[3]}.{m[2]}.{m[1]}"
    m = re.match(r"^(\d{2})\.(\d{2})\.(20\d{2})", s)
    return m.group(0) if m else ""


def telegram(text):
    bot_token = env_str("TG_DELIVERY")
    chat = env_str("TG_TARGET")
    if not bot_token or not chat:
        print("! TG_DELIVERY / TG_TARGET не заданы — сообщение не отправлено")
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "false"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"telegram: HTTP {resp.status}")
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"! telegram ошибка: {type(exc).__name__}: {exc}")
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    state["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001
        print(f"! не смог сохранить состояние: {exc}")


def main():
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"СБОЙ: playwright недоступен ({exc})")
        return 0

    captured = []
    title = ""
    info = {}
    last_err = ""

    with sync_playwright() as pw:
        launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = pw.chromium.launch(headless=True, args=launch_args, **kwargs)
                info["channel"] = kwargs.get("channel", "chromium")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if browser is None:
            print(f"СБОЙ: Chrome/Chromium не запустился ({last_err})")
            return 0
        context = browser.new_context(
            user_agent=UA,
            locale="uk-UA",
            extra_http_headers={"accept-language": "uk-UA,uk;q=0.9,en-US;q=0.8"},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
            "window.chrome=window.chrome||{runtime:{}};"
        )
        page = context.new_page()

        def on_response(resp):
            if "/solutions/e-queue" in resp.url and resp.request.method == "POST":
                try:
                    captured.append(resp.text()[:4000])
                except Exception:  # noqa: BLE001
                    captured.append("")

        page.on("response", on_response)

        try:
            page.goto(PAGE, wait_until="domcontentloaded", timeout=120000)
        except Exception as exc:  # noqa: BLE001
            print(f"СБОЙ: не открылась страница ({type(exc).__name__}: {exc})")
            browser.close()
            return 0

        deadline = time.time() + 120
        while time.time() < deadline:
            title = page.title() or ""
            if title and not CHALLENGE_TITLE.search(title):
                break
            time.sleep(3)

        if not title or CHALLENGE_TITLE.search(title):
            print(f"СБОЙ: Cloudflare челлендж не пройден (title={title!r}) — не «нет мест»")
            save_state({**load_state(), "state": "blocked"})
            browser.close()
            return 0

        page.wait_for_timeout(4000)

        # выбор услуги — как делает человек
        try:
            page.evaluate(
                """(v) => {
                    const s = document.querySelector('#service');
                    if (!s) return 'no';
                    s.value = v;
                    s.dispatchEvent(new Event('input', { bubbles: true }));
                    s.dispatchEvent(new Event('change', { bubbles: true }));
                    return s.value;
                }""",
                SERVICE_VALUE,
            )
            info["service_set"] = "value=" + SERVICE_VALUE
        except Exception as exc:  # noqa: BLE001
            info["service_set"] = f"ошибка: {exc}"

        deadline = time.time() + 45
        days = None
        while time.time() < deadline:
            for body in captured:
                try:
                    parsed = json.loads(body)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(parsed, dict) and isinstance(parsed.get("days"), list):
                    days = parsed["days"]
                    break
            if days is not None:
                break
            time.sleep(2)

        if days is None:
            try:
                info["busy_body"] = page.evaluate(
                    """() => /всі місця зайняті|спробуйте записатись пізніше/i.test(document.body.innerText)"""
                )
                info["date_options"] = page.evaluate(
                    """() => { const s = document.querySelector('#date');
                        return s ? Array.from(s.options).map(o => (o.textContent||'').trim())
                            .filter(x => x && !/обрати/i.test(x)) : []; }"""
                )
            except Exception:  # noqa: BLE001
                pass
        browser.close()

    dates = []
    seats = 0
    for day in days or []:
        d = normalize_date(day.get("date") or day.get("datePart") or day.get("day"))
        if d:
            dates.append(d)
        count = day.get("allowedJobCount", day.get("slots"))
        if isinstance(count, int):
            seats += count

    if days is None and info.get("date_options"):
        dates = list(dict.fromkeys(info["date_options"]))

    state = load_state()
    if dates:
        lines = ["🎉 ВІЛЬНІ МІСЦЯ В ПАСПОРТНОМУ СЕРВІСІ!", "", "📍 München", "📅 Дати зі слотами:"]
        lines += [f"  • {d}" for d in dates[:12]]
        if seats:
            lines.append(f"🗂 Всього місць у відповіді: {seats}")
        lines += [f"🕐 Перевірено: {stamp()}", "", f"👉 {PAGE}"]

        last = state.get("last_alert_utc", "")
        cooled = True
        if last:
            try:
                cooled = (
                    dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)
                ).total_seconds() >= COOLDOWN_HOURS * 3600
            except Exception:  # noqa: BLE001
                cooled = True
        if cooled:
            print("МЕСТА ЕСТЬ: " + ", ".join(dates))
            telegram("\n".join(lines))
            state["last_alert_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        else:
            print(f"МЕСТА ЕСТЬ ({', '.join(dates)}), но кулдаун ещё не истёк")
        state["state"] = "open"
    elif days is not None:
        print(f"Мест нет (ответ приложения days=[]), проверено {stamp()}")
        state["state"] = "all-taken"
    elif info.get("busy_body"):
        print(f"Мест нет (страница показала «всі місця зайняті»), проверено {stamp()}")
        state["state"] = "all-taken"
    else:
        print(f"НЕОПРЕДЕЛЁННО: {json.dumps(info, ensure_ascii=False)} — ничего не отправляю")
        state["state"] = "unknown"

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
