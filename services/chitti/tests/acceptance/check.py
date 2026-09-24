"""Run against the isolated Compose fixture, never a deployed PRISM environment."""
import subprocess
import time
from pathlib import Path

import httpx

COMPOSE = ["docker", "compose", "-p", "prism-chitti-acceptance",
           "-f", str(Path(__file__).with_name("compose.yml"))]
BASE = "http://127.0.0.1:18439"


def compose(*args):
    return subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True).stdout  # noqa: S603 - fixed fixture commands


def sql(statement):
    return subprocess.run(  # noqa: S603 - fixed fixture commands
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "prism", "-d", "access",
         "-At", "-v", "ON_ERROR_STOP=1", "-c", statement],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def token(email):
    response = httpx.post(f"{BASE}/dex/token", data={
        "grant_type": "password", "client_id": "prism", "scope": "openid email profile",
        "username": email, "password": "prism",
    })
    response.raise_for_status()
    return response.json()["id_token"]


def chat(bearer, *, tenant="EVAM", question="read", stream=False):
    return httpx.post(f"{BASE}/chitti/v1/chat/completions", timeout=15, headers={
        "Authorization": f"Bearer {bearer}", "X-Tenant": tenant,
    }, json={"model": "prism-chitti", "messages": [{"role": "user", "content": question}],
             "stream": stream})


def answer(response):
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def main():
    scoped_email = "e2e.rm@evamfinance.com"
    sql("UPDATE users SET is_active=true WHERE email='e2e.rm@evamfinance.com'")
    time.sleep(2.2)
    assert sql("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='register_app'") == "f"
    compose("exec", "-T", "register", "python", "-c", """
import asyncio
import asyncpg
from app.core.config import get_settings
async def check():
    s = get_settings()
    c = await asyncpg.connect(host=s.db_host, port=s.db_port, database=s.db_name,
                              user=s.db_user, password=s.db_password)
    try:
        row = await c.fetchrow('SELECT current_user, rolsuper, rolbypassrls '
                               'FROM pg_roles WHERE rolname=current_user')
        assert tuple(row) == ('register_app', False, False), tuple(row)
        assert await c.fetchval('SELECT count(*) FROM lending_tracker') == 0
    finally:
        await c.close()
asyncio.run(check())
""")
    admin, scoped, denied = [token(email) for email in (
        "admin@evamfinance.com", scoped_email, "e2e.maker@evamfinance.com")]
    full = answer(chat(admin))
    assert "Accessible facilities: 2." in full, full
    assert "amber" in full and "violet" in full and "crimson" not in full, full
    limited = answer(chat(scoped))
    assert "Accessible facilities: 1." in limited, limited
    assert "amber" in limited and "violet" not in limited and "crimson" not in limited, limited
    print("PASS restricted runtime, scoped records, aggregates, qualitative corpus")
    for response in (chat(denied), chat(scoped, tenant="OTHER"), chat("forged")):
        if response.status_code == 200:
            assert response.json()["chitti"]["outcome"] == "FAILED", response.text
            assert not response.json()["chitti"].get("evidence"), response.text
            assert not any(word in response.text for word in ("amber", "violet", "crimson"))
        else:
            assert response.status_code in (401, 403), response.text
    print("PASS denied user, cross-tenant membership, forged identity")
    compose("exec", "-T", "chitti", "python", "-c",
            "import httpx; "
            "assert httpx.get('http://register:8000/v1/lending', "
            "headers={'X-API-Key':'acceptance-chitti'}).status_code == 403; "
            "assert httpx.post('http://localhost:8000/v1/chat/completions', "
            "headers={'X-API-Key':'acceptance-front'}, json={'model':'prism-chitti',"
            "'messages':[{'role':'user','content':'read'}]}).status_code == 403")
    print("PASS service-only credentials cannot read ledger or run delegated chat")
    timed = chat(scoped, question="wait", stream=True)
    timed.raise_for_status()
    assert "[DONE]" in timed.text and "I couldn't complete" in timed.text, timed.text
    assert "Accessible facilities: 1." in answer(chat(scoped))
    print("PASS timeout terminates stream and releases request capacity")
    for _ in range(10):
        with httpx.stream("POST", f"{BASE}/chitti/v1/chat/completions", timeout=10,
                          headers={"Authorization": f"Bearer {scoped}"}, json={
                              "model": "prism-chitti", "stream": True,
                              "messages": [{"role": "user", "content": "wait"}],
                          }) as response:
            response.raise_for_status()
            next(response.iter_lines())
        time.sleep(0.1)
    assert "Accessible facilities: 1." in answer(chat(scoped))
    print("PASS repeated edge disconnects leave capacity available")
    for service in ("register", "access"):
        compose("stop", service)
        try:
            time.sleep(2.2)
            response = chat(scoped)
            if service == "register":
                assert response.json()["chitti"]["outcome"] == "FAILED", response.text
                assert not response.json()["chitti"].get("evidence"), response.text
            else:
                assert response.status_code in (502, 503), response.text
        finally:
            compose("start", service)
        for _attempt in range(30):
            time.sleep(1)
            try:
                if "Accessible facilities: 1." in answer(chat(scoped)):
                    break
            except (httpx.HTTPError, KeyError):
                pass
        else:
            raise AssertionError(f"{service} did not recover")
        print(f"PASS {service} outage fails closed and recovers through edge")
    sql("UPDATE users SET is_active=false, permissions_epoch=permissions_epoch+1 "
        "WHERE email='e2e.rm@evamfinance.com'")
    try:
        time.sleep(2.2)
        revoked = chat(scoped)
        assert revoked.status_code in (401, 403), revoked.text
        print("PASS deactivation after configured cache expiry")
    finally:
        sql("UPDATE users SET is_active=true WHERE email='e2e.rm@evamfinance.com'")


if __name__ == "__main__":
    main()
