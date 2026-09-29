"""Testes das rotas de leitura contra um PostgreSQL real.

Antes estes caminhos eram exercitados apenas com dublês de sessão, o que deixava
fora a paginação por cursor, a janela `around` e a busca FTS — justamente o que
depende de índice e de semântica do PostgreSQL.
"""

from datetime import UTC, datetime, timedelta

from backend.app.models import Chat, Message
from tests.support import requires_database

pytestmark = requires_database

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _seed(session, conversations: int = 3, per_chat: int = 5) -> None:
    for chat_index in range(conversations):
        jid = f"chat{chat_index}@s.whatsapp.net"
        session.add(
            Chat(
                jid=jid,
                name=f"Contato {chat_index}",
                is_group=chat_index % 2 == 0,
                created_at=BASE,
                last_message_time=BASE + timedelta(minutes=per_chat * chat_index),
            )
        )
        for message_index in range(per_chat):
            session.add(
                Message(
                    id=f"m{chat_index}-{message_index}",
                    chat_jid=jid,
                    sender_name="Ana" if message_index % 2 else "Você",
                    content=f"mensagem {message_index} conversa {chat_index}",
                    timestamp=BASE + timedelta(minutes=chat_index * 100 + message_index),
                    from_me=message_index % 2 == 0,
                    has_media=False,
                )
            )
    session.commit()


def test_chats_are_ordered_by_last_message_and_filtered_by_group(client, session):
    _seed(session, conversations=3, per_chat=5)

    listed = client.get("/api/chats?limit=10").json()
    assert [chat["jid"] for chat in listed] == [
        "chat2@s.whatsapp.net",
        "chat1@s.whatsapp.net",
        "chat0@s.whatsapp.net",
    ]
    assert listed[0]["last_message_preview"].startswith("mensagem 4")

    groups = client.get("/api/chats?is_group=true").json()
    assert [chat["jid"] for chat in groups] == [
        "chat2@s.whatsapp.net",
        "chat0@s.whatsapp.net",
    ]
    assert client.get("/api/chats?q=Contato 1").json()[0]["jid"] == "chat1@s.whatsapp.net"


def test_chat_pagination_uses_cursor_without_repeating_messages(client, session):
    _seed(session, conversations=1, per_chat=25)

    first = client.get("/api/chats/chat0@s.whatsapp.net/messages?limit=10").json()
    assert len(first["items"]) == 10
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = client.get(
        "/api/chats/chat0@s.whatsapp.net/messages",
        params={"before": first["next_cursor"], "limit": 10},
    ).json()
    identifiers = {message["id"] for message in first["items"]} & {
        message["id"] for message in second["items"]
    }
    assert not identifiers
    assert len(second["items"]) == 10

    # A última página não oferece cursor, sinalizando o fim do histórico.
    last = client.get(
        "/api/chats/chat0@s.whatsapp.net/messages",
        params={"before": second["next_cursor"], "limit": 10},
    ).json()
    assert last["has_more"] is False
    assert last["next_cursor"] is None
    assert len(last["items"]) == 5


def test_messages_around_returns_a_window_containing_the_target(client, session):
    """`around` é o caminho do "ir para a mensagem": a janela precisa conter o
    alvo e crescer para os dois lados, sem ultrapassar o limite pedido."""
    _seed(session, conversations=2, per_chat=30)

    page = client.get(
        "/api/chats/chat0@s.whatsapp.net/messages", params={"around": "m0-20", "limit": 20}
    ).json()
    identifiers = [message["id"] for message in page["items"]]

    # Metade do limite para trás (incluindo o alvo) e o resto para frente. Só
    # existem 9 mensagens depois de m0-20, então a janela fecha em 19.
    assert identifiers == sorted(identifiers)
    assert identifiers.index("m0-20") == 9
    assert identifiers[0] == "m0-11"
    assert identifiers[-1] == "m0-29"
    assert len(identifiers) <= 20

    # No meio da conversa a janela enche o limite inteiro.
    middle = client.get(
        "/api/chats/chat0@s.whatsapp.net/messages", params={"around": "m0-15", "limit": 20}
    ).json()["items"]
    assert len(middle) == 20
    assert [message["id"] for message in middle].index("m0-15") == 9

    assert (
        client.get(
            "/api/chats/chat0@s.whatsapp.net/messages", params={"around": "inexistente"}
        ).status_code
        == 404
    )
    # O id precisa pertencer à conversa pedida, senão `around` vaza entre chats.
    assert (
        client.get(
            "/api/chats/chat0@s.whatsapp.net/messages", params={"around": "chat1-5"}
        ).status_code
        == 404
    )


def test_search_finds_messages_and_highlights_without_injecting_html(client, session):
    _seed(session, conversations=2, per_chat=5)
    session.add(
        Message(
            id="alvo",
            chat_jid="chat0@s.whatsapp.net",
            sender_name="Ana",
            content="relatório anual do projeto <script>alert(1)</script>",
            timestamp=BASE + timedelta(hours=1),
            from_me=False,
            has_media=False,
        )
    )
    session.commit()

    found = client.get("/api/search", params={"q": "projeto"}).json()
    assert found["items"]
    assert found["items"][0]["message"]["id"] == "alvo"
    assert "<mark>" in found["items"][0]["highlight"]

    scoped = client.get(
        "/api/search", params={"q": "projeto", "chat_jid": "chat1@s.whatsapp.net"}
    ).json()
    assert scoped["items"] == []

    # Uma palavra muito curta é recusada pela validação, não pelo PostgreSQL.
    assert client.get("/api/search", params={"q": "a"}).status_code == 422


def test_messages_expose_quotes_and_media_urls(client, session):
    session.add(Chat(jid="c@s.whatsapp.net", name="C", is_group=False, created_at=BASE))
    session.add(
        Message(
            id="original",
            chat_jid="c@s.whatsapp.net",
            sender_name="Ana",
            content="olá",
            timestamp=BASE,
            from_me=False,
            has_media=False,
        )
    )
    session.add(
        Message(
            id="resposta",
            chat_jid="c@s.whatsapp.net",
            sender_name="Você",
            content="resposta à mensagem",
            timestamp=BASE + timedelta(minutes=1),
            from_me=True,
            has_media=True,
            media_type="image",
            media_path="c@s.whatsapp.net/2026_09/foto espaco.jpg",
            quoted_message_id="original",
        )
    )
    session.commit()

    page = client.get("/api/chats/c@s.whatsapp.net/messages?limit=10").json()
    reply = next(item for item in page["items"] if item["id"] == "resposta")

    assert reply["quoted_message"]["id"] == "original"
    assert reply["quoted_message"]["content"] == "olá"
    # O caminho é percent-encoded e nunca pode escapar do volume de mídia.
    assert reply["media_url"] == "/media/c%40s.whatsapp.net/2026_09/foto%20espaco.jpg"
    assert reply["thumbnail_url"].endswith(".thumbs/foto%20espaco.jpg.jpg")


def test_media_route_blocks_path_traversal(client):
    assert client.get("/media/../../etc/passwd").status_code in {400, 404}
    assert client.get("/media/%2e%2e%2f%2e%2e%2fetc%2fpasswd").status_code in {400, 404}


def test_health_and_security_headers(client):
    response = client.get("/api/health")

    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
