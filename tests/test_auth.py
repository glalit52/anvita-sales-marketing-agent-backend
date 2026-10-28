async def test_signup_me_and_login(client, ws):
    me = await ws.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "priya@smiledental.in"
    assert body["org"]["role"] == "owner"
    assert body["org"]["slug"] == "smile-dental"

    bad = await client.post("/auth/login", json={"email": "priya@smiledental.in", "password": "wrong-password"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"

    ok = await client.post("/auth/login", json={"email": "PRIYA@smiledental.in", "password": "correct-horse-battery"})
    assert ok.status_code == 200
    assert ok.json()["org"]["id"] == ws.org["id"]


async def test_duplicate_signup_rejected(client, ws):
    r = await client.post(
        "/auth/signup",
        json={
            "name": "X",
            "email": "priya@smiledental.in",
            "password": "another-long-password",
            "businessName": "Other",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


async def test_refresh_rotation_and_reuse_detection(client, ws):
    first = ws.session["refreshToken"]
    r1 = await client.post("/auth/refresh", json={"refreshToken": first})
    assert r1.status_code == 200
    second = r1.json()["refreshToken"]
    assert second != first

    # Replaying the rotated token is treated as theft: it fails and revokes the live token too.
    replay = await client.post("/auth/refresh", json={"refreshToken": first})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_reused"
    assert (await client.post("/auth/refresh", json={"refreshToken": second})).status_code == 401


async def test_requires_auth(client):
    r = await client.get("/contacts")
    assert r.status_code == 401
    r = await client.get("/contacts", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_invite_flow_and_roles(client, ws):
    inv = await ws.post("/invites", {"email": "arjun@smiledental.in", "role": "agent"})
    assert inv.status_code == 201, inv.text
    token = inv.json()["inviteUrl"].split("invite=")[1]

    joined = await client.post(
        "/auth/accept-invite", json={"token": token, "name": "Arjun Patil", "password": "arjun-long-password"}
    )
    assert joined.status_code == 200, joined.text
    agent_headers = {"Authorization": f"Bearer {joined.json()['accessToken']}"}
    assert joined.json()["org"]["role"] == "agent"

    # Agents cannot change workspace settings or invite people.
    assert (await client.patch("/org", json={"name": "Hacked"}, headers=agent_headers)).status_code == 403
    assert (await client.post("/invites", json={"email": "x@y.in"}, headers=agent_headers)).status_code == 403

    team = (await ws.get("/team")).json()
    assert {m["email"] for m in team} == {"priya@smiledental.in", "arjun@smiledental.in"}

    # Invite tokens are single use.
    again = await client.post("/auth/accept-invite", json={"token": token, "password": "arjun-long-password"})
    assert again.status_code == 404


def test_cors_origins_from_comma_separated_env(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("ANVITA_CORS_ORIGINS", "http://localhost:3000, https://app.anvita.ai")
    assert Settings().cors_origins == ["http://localhost:3000", "https://app.anvita.ai"]
    monkeypatch.setenv("ANVITA_CORS_ORIGINS", '["https://a.example"]')
    assert Settings().cors_origins == ["https://a.example"]
