from datetime import UTC, datetime

from ingestor.chat_export import WhatsAppExportReader


def test_reads_android_export_and_infers_owner(tmp_path):
    transcript = tmp_path / "Conversa do WhatsApp com Ana.txt"
    transcript.write_text(
        "01/09/2026 10:00 - Ana: Olá\n"
        "01/09/2026 10:01 - Fabio: Primeira linha\n"
        "segunda linha\n"
        "01/09/2026 10:02 - Ana: IMG-20260901-WA0001.jpg (arquivo anexado)\n",
        encoding="utf-8",
    )

    reader = WhatsAppExportReader([transcript])
    chats = reader.chats()
    messages = list(reader.iter_messages())

    assert chats[0].name == "Ana"
    assert not chats[0].is_group
    assert chats[0].created_at == datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
    assert messages[0].sender_name == "Ana"
    assert not messages[0].from_me
    assert messages[1].content == "Primeira linha\nsegunda linha"
    assert messages[1].from_me
    assert messages[2].media_reference == "IMG-20260901-WA0001.jpg"
    assert messages[2].media_mime == "image/jpeg"
    assert messages[2].content is None


def test_reads_ios_export_with_explicit_month_day_order(tmp_path):
    transcript = tmp_path / "_chat.txt"
    transcript.write_text(
        "[9/1/26, 10:15:03 PM] John: Hello\n"
        "[9/1/26, 10:16:00 PM] Me: <attached: 00000001-PHOTO.jpg>\n",
        encoding="utf-8",
    )

    reader = WhatsAppExportReader(
        [transcript],
        timezone="UTC",
        date_order="mdy",
        owner_name="Me",
        fallback_title="WhatsApp Chat with John.zip",
    )
    messages = list(reader.iter_messages())

    assert reader.chats()[0].name == "John"
    assert messages[0].timestamp == datetime(2026, 9, 1, 22, 15, 3, tzinfo=UTC)
    assert messages[1].from_me
    assert messages[1].has_media
    assert messages[1].media_reference == "00000001-PHOTO.jpg"


def test_export_message_ids_are_stable_and_distinguish_identical_occurrences(tmp_path):
    transcript = tmp_path / "Equipe.txt"
    transcript.write_text(
        "01/09/2026, 10:00 - Ana: repetida\n01/09/2026, 10:00 - Ana: repetida\n",
        encoding="utf-8",
    )

    first = [message.id for message in WhatsAppExportReader([transcript]).iter_messages()]
    second = [message.id for message in WhatsAppExportReader([transcript]).iter_messages()]

    assert first == second
    assert len(set(first)) == 2


def test_reads_utf16_export(tmp_path):
    transcript = tmp_path / "Contato.txt"
    transcript.write_text(
        "01/09/2026 10:00 - Contato: Olá em UTF-16\n",
        encoding="utf-16",
    )

    messages = list(WhatsAppExportReader([transcript], timezone="UTC").iter_messages())

    assert messages[0].content == "Olá em UTF-16"
