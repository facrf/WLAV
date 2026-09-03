import sqlite3
from datetime import UTC

from ingestor.sqlite_reader import MsgstoreReader, normalize_timestamp


def test_normalize_timestamp_accepts_common_epoch_precisions():
    expected = normalize_timestamp(1_700_000_000)
    assert expected is not None
    assert expected.tzinfo is UTC
    assert normalize_timestamp(1_700_000_000_000) == expected
    assert normalize_timestamp(1_700_000_000_000_000) == expected


def test_reads_legacy_android_schema(tmp_path):
    database = tmp_path / "msgstore.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE chat_list (
            key_remote_jid TEXT PRIMARY KEY,
            subject TEXT,
            creation INTEGER,
            sort_timestamp INTEGER
        );
        CREATE TABLE wa_contacts (jid TEXT, display_name TEXT);
        CREATE TABLE messages (
            _id INTEGER PRIMARY KEY,
            key_remote_jid TEXT,
            key_id TEXT,
            remote_resource TEXT,
            key_from_me INTEGER,
            timestamp INTEGER,
            data TEXT,
            media_caption TEXT,
            media_wa_type INTEGER,
            media_name TEXT,
            mime_type TEXT,
            quoted_row_id INTEGER
        );
        INSERT INTO chat_list VALUES ('5511999999999@s.whatsapp.net', NULL, 1699999000000, 1700000000000);
        INSERT INTO wa_contacts VALUES ('5511999999999@s.whatsapp.net', 'Cliente Exemplo');
        INSERT INTO messages VALUES (1, '5511999999999@s.whatsapp.net', 'AAA', NULL, 0, 1700000000000, 'Olá', NULL, 0, NULL, NULL, NULL);
        INSERT INTO messages VALUES (2, '5511999999999@s.whatsapp.net', 'BBB', NULL, 1, 1700000001000, NULL, 'Veja o PDF', 9, 'DOC-001.pdf', 'application/pdf', 1);
        """
    )
    connection.close()

    with MsgstoreReader(database) as reader:
        assert reader.schema == "legacy"
        chats = reader.chats()
        messages = list(reader.iter_messages(batch_size=1))

    assert chats[0].name == "Cliente Exemplo"
    assert not chats[0].is_group
    assert messages[0].content == "Olá"
    assert messages[1].from_me
    assert messages[1].media_type == "document"
    assert messages[1].media_reference == "DOC-001.pdf"
    assert messages[1].quoted_message_id == "AAA"


def test_reads_modern_android_schema(tmp_path):
    database = tmp_path / "msgstore.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, user TEXT, server TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, created_timestamp INTEGER, sort_timestamp INTEGER);
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY,
            chat_row_id INTEGER,
            key_id TEXT,
            sender_jid_row_id INTEGER,
            from_me INTEGER,
            timestamp INTEGER,
            text_data TEXT,
            message_type INTEGER
        );
        CREATE TABLE message_media (
            message_row_id INTEGER,
            file_path TEXT,
            media_mime_type TEXT,
            media_caption TEXT
        );
        CREATE TABLE message_quoted (message_row_id INTEGER, key_id TEXT);
        INSERT INTO jid VALUES (1, '120000000000@g.us', NULL, NULL);
        INSERT INTO jid VALUES (2, '5511888888888@s.whatsapp.net', NULL, NULL);
        INSERT INTO chat VALUES (5, 1, 'Equipe', 1699999000000, 1700000000000);
        INSERT INTO message VALUES (10, 5, 'MODERN-1', 2, 0, 1700000000000, NULL, 1);
        INSERT INTO message_media VALUES (10, 'WhatsApp Images/IMG-001.jpg', 'image/jpeg', 'Uma foto');
        INSERT INTO message_quoted VALUES (10, 'OLD-1');
        """
    )
    connection.close()

    with MsgstoreReader(database) as reader:
        messages = list(reader.iter_messages())
        chats = reader.chats()

    assert chats[0].is_group
    assert chats[0].name == "Equipe"
    assert messages[0].sender_jid == "5511888888888@s.whatsapp.net"
    assert messages[0].sender_name == "5511888888888"
    assert messages[0].content == "Uma foto"
    assert messages[0].media_type == "image"
    assert messages[0].quoted_message_id == "OLD-1"
