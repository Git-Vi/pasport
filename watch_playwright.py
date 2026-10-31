#!/usr/bin/env python3
"""DP «Документ» Мюнхен — монитор слотов через реальный Chrome на GitHub Actions.

Две проверки в одном прогоне:
  1) plain_check()   — обычный HTTP-запрос: что именно отдаёт сайт раннеру (статус, длина, челлендж).
  2) browser_check() — реальный Chrome: ждёт прохождения Cloudflare, выбирает услугу и
     перехватывает ответ приложения {"days": [...]} (тело запроса знать не нужно).

Секреты репозитория: TG_DELIVERY (токен бота), TG_TARGET (id чата).
Ничего не бронирует и персональные данные не отправляет.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
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


def plain_check():
    """Что отдаёт сайт обычному HTTP-клиенту с этого раннера."""
    info = {}
    req = urllib.request.Request(
        PAGE,
        headers={
            "user-agent": UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "uk-UA,uk;q=0.9,en-US;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read()
            text = body.decode("utf-8", errors="ignore")
            info["http_status"] = resp.status
            info["cf_mitigated"] = resp.headers.get("cf-mitigated", "")
            info["len"] = len(body)
            m = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
            info["title"] = (m.group(1).strip()[:80] if m else "")
            info["challenge"] = bool(CHALLENGE_TITLE.search(text))
            info["csrf"] = bool(re.search(r"csrf&quot;:&quot;([a-f0-9]{16,})", text))
            info["head"] = text[:180].replace("\n", " ")
    except urllib.error.HTTPError as exc:
        info["http_status"] = exc.code
        info["cf_mitigated"] = (exc.headers.get("cf-mitigated", "") if exc.headers else "")
        info["error"] = f"HTTPError {exc.code}"
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def browser_check():
    """Chrome: ждём Cloudflare, выбираем услугу, перехватываем ответ приложения."""
    out = {"days": None, "title": "", "dates_dom": [], "busy": False, "diag": {}}
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        out["diag"]["playwright"] = f"недоступен: {exc}"
        return out

    captured = []
    with sync_playwright() as pw:
        launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        browser = None
        last_err = ""
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = pw.chromium.launch(headless=True, args=launch_args, **kwargs)
                out["diag"]["channel"] = kwargs.get("channel", "chromium")
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
        if browser is None:
            out["diag"]["launch_error"] = last_err
            return out

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
            resp = page.goto(PAGE, wait_until="domcontentloaded", timeout=120000)
            if resp is not None:
                out["diag"]["goto_status"] = resp.status
                out["diag"]["goto_cf"] = resp.headers.get("cf-mitigated", "")
                out["diag"]["goto_ctype"] = (resp.headers.get("content-type") or "")[:60]
                try:
                    out["diag"]["goto_len"] = len(resp.body())
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            out["diag"]["goto_error"] = f"{type(exc).__name__}: {exc}"

        deadline = time.time() + 90
        while time.time() < deadline:
            out["title"] = page.title() or ""
            if out["title"] and not CHALLENGE_TITLE.search(out["title"]):
                break
            time.sleep(3)

        try:
            out["diag"]["dom_len"] = page.evaluate("() => document.documentElement.outerHTML.length")
            out["diag"]["url_now"] = page.url
            out["diag"]["body_head"] = (
                page.evaluate("() => document.body ? document.body.innerText.slice(0,150) : ''") or ""
            ).replace("\n", " ")
        except Exception as exc:  # noqa: BLE001
            out["diag"]["dom_error"] = f"{type(exc).__name__}: {exc}"

        if out["title"] and not CHALLENGE_TITLE.search(out["title"]):
            page.wait_for_timeout(4000)
            try:
                page.evaluate(
                    """(v) => { const s = document.querySelector('#service');
                        if (!s) return 'no'; s.value = v;
                        s.dispatchEvent(new Event('input', { bubbles: true }));
                        s.dispatchEvent(new Event('change', { bubbles: true })); return s.value; }""",
                    SERVICE_VALUE,
                )
                out["diag"]["service_set"] = SERVICE_VALUE
            except Exception as exc:  # noqa: BLE001
                out["diag"]["service_set"] = f"ошибка: {exc}"

            deadline = time.time() + 40
            while time.time() < deadline and out["days"] is None:
                for body in captured:
                    try:
                        parsed = json.loads(body)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(parsed, dict) and isinstance(parsed.get("days"), list):
                        out["days"] = parsed["days"]
                        break
                if out["days"] is None:
                    time.sleep(2)

            if out["days"] is None:
                try:
                    out["busy"] = bool(page.evaluate(
                        "() => /всі місця зайняті|спробуйте записатись пізніше/i.test(document.body.innerText)"
                    ))
                    out["dates_dom"] = page.evaluate(
                        """() => { const s = document.querySelector('#date');
                            return s ? Array.from(s.options).map(o => (o.textContent||'').trim())
                                .filter(x => x && !/обрати/i.test(x)) : []; }"""
                    )
                except Exception:  # noqa: BLE001
                    pass

        browser.close()
    return out


def main():
    plain = plain_check()
    print("PLAIN: " + json.dumps(plain, ensure_ascii=False))

    browser = browser_check()
    print("BROWSER: " + json.dumps(
        {"title": browser["title"], "days": browser["days"], "busy": browser["busy"],
         "dates_dom": browser["dates_dom"], "diag": browser["diag"]},
        ensure_ascii=False,
    ))

    days = browser["days"]
    dates = []
    seats = 0
    for day in days or []:
        d = normalize_date(day.get("date") or day.get("datePart") or day.get("day"))
        if d:
            dates.append(d)
        count = day.get("allowedJobCount", day.get("slots"))
        if isinstance(count, int):
            seats += count
    if not dates and browser["dates_dom"]:
        dates = list(dict.fromkeys(browser["dates_dom"]))

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
    elif days is not None or browser["busy"]:
        print(f"Мест нет (ответ приложения или текст страницы), проверено {stamp()}")
        state["state"] = "all-taken"
    elif plain.get("challenge") or plain.get("http_status") in (403, 429):
        print(
            f"СБОЙ: сайт не пускает раннер (HTTP {plain.get('http_status')}, "
            f"cf-mitigated='{plain.get('cf_mitigated', '')}') — не «нет мест»"
        )
        state["state"] = "blocked"
    else:
        print("НЕОПРЕДЕЛЁННО: страница без заголовка, данные не получены — не «нет мест»")
        state["state"] = "unknown"

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
