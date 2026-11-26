"""Checks that each pull request merged into the experimental branch works.

Each test is tied to its pull request. If a build leaves a pull request out (merged upstream, closed,
or conflicting), build.json says so and its tests are skipped instead of failing.
"""

from __future__ import annotations

import base64
import datetime
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord
from discord.audit_logs import AuditLogEntry

MANIFEST = Path(__file__).resolve().parent.parent / 'build.json'
INCLUDED = {pr['number'] for pr in json.loads(MANIFEST.read_text())['included']} if MANIFEST.exists() else None


def requires(pr: int):
    return pytest.mark.skipif(INCLUDED is not None and pr not in INCLUDED, reason=f'PR #{pr} is not in this build')


def fake_state():
    state = MagicMock()
    state.allowed_mentions = None
    state.http.send_message = AsyncMock(return_value={'id': '1'})
    state.create_message = lambda channel, data: SimpleNamespace(id=int(data['id']))
    return state


def fake_guild(state):
    guild = MagicMock(spec=discord.Guild)
    guild.id = 100
    guild._state = state
    return guild


def text_channel(state=None, **data):
    state = state or fake_state()
    payload = {'id': '200', 'name': 'general', 'type': 0, 'position': 0, 'guild_id': '100', **data}
    return discord.TextChannel(state=state, guild=fake_guild(state), data=payload)  # pyright: ignore[reportArgumentType]


def sent_payload(state) -> dict:
    params = state.http.send_message.await_args.kwargs['params']
    if params.multipart:
        return json.loads(next(p['value'] for p in params.multipart if p['name'] == 'payload_json'))
    return params.payload


# PR #10513: identify properties


@requires(10513)
@pytest.mark.asyncio
async def test_identify_sends_the_configured_properties(monkeypatch):
    from discord.gateway import DiscordWebSocket, _set_identify_properties

    default = dict(DiscordWebSocket.IDENTIFY_PROPERTIES)
    assert set(default) == {'os', 'browser', 'device'}
    monkeypatch.setattr(DiscordWebSocket, 'IDENTIFY_PROPERTIES', default)
    _set_identify_properties({'os': 'linux', 'browser': 'Discord iOS', 'device': 'Discord iOS'})

    ws = DiscordWebSocket.__new__(DiscordWebSocket)
    ws.token, ws.shard_id, ws.shard_count, ws._initial_identify = 'token', None, None, True
    ws._connection = SimpleNamespace(_activity=None, _status=None, _intents=None)
    ws.call_hooks = AsyncMock()
    ws.send_as_json = AsyncMock()
    await ws.identify()
    assert ws.send_as_json.await_args.args[0]['d']['properties']['browser'] == 'Discord iOS'

    with pytest.raises(TypeError):
        _set_identify_properties(['not', 'a', 'dict'])  # pyright: ignore[reportArgumentType]


# PR #10495: VR client status


@requires(10495)
def test_vr_client_status():
    from discord.presences import ClientStatus

    status = ClientStatus(status='online', data={'vr': 'idle', 'desktop': 'online'})
    assert status.vr_status is discord.Status.idle and status.desktop_status is discord.Status.online
    assert ClientStatus(status='online', data={}).vr_status is discord.Status.offline
    assert isinstance(discord.Member.vr_status, property)


# PR #10545: fetch member and role permissions


@requires(10545)
@pytest.mark.asyncio
async def test_fetch_permissions_for_guild_and_channel():
    state = fake_state()
    state.http.get_guild_member_permissions = AsyncMock(return_value={'permissions': '8'})
    state.http.get_channel_role_permissions = AsyncMock(return_value={'permissions': '2048'})

    guild = discord.Guild.__new__(discord.Guild)
    guild.id, guild._state = 100, state
    assert await guild.fetch_permissions_for(discord.Object(id=5, type=discord.Member)) == discord.Permissions(8)
    state.http.get_guild_member_permissions.assert_awaited_once_with(100, 5)

    channel = text_channel(state)
    perms = await channel.fetch_permissions_for(discord.Object(id=7, type=discord.Role), implicit=False)
    assert perms.send_messages
    assert state.http.get_channel_role_permissions.await_args.kwargs == {'implicit': False}

    with pytest.raises(TypeError):
        await guild.fetch_permissions_for(discord.Object(id=1))


# PR #10546: new attachment flags


@requires(10546)
def test_new_attachment_flags():
    assert discord.AttachmentFlags._from_value(1 << 6).contains_gore_media
    assert discord.AttachmentFlags._from_value(1 << 7).contains_self_harm_media
    assert not discord.AttachmentFlags._from_value(0).contains_gore_media


# PR #10517 (+ fix patch): probed bitrate


@requires(10517)
@pytest.mark.parametrize('bit_rate, expected', [(128000, 128), (900000, 512), (None, None)])
def test_probed_bitrate_is_capped_and_unknown_is_none(monkeypatch, bit_rate, expected):
    import subprocess

    stream = {'codec_name': 'opus', **({'bit_rate': str(bit_rate)} if bit_rate else {})}
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: json.dumps({'streams': [stream]}).encode())
    assert discord.FFmpegOpusAudio._probe_codec_native('song.webm') == ('opus', expected)


# PR #10230: voice messages


@requires(10230)
@pytest.mark.asyncio
async def test_voice_message_file_and_send():
    file = discord.File(io.BytesIO(b'audio'), filename='voice.ogg', duration=2.5, waveform=[0, 128, 255])
    assert file.voice
    payload = file.to_dict(0)
    assert payload['duration_secs'] == 2.5 and base64.b64decode(payload['waveform']) == bytes([0, 128, 255])

    state = fake_state()
    await text_channel(state).send(file=file)
    assert discord.MessageFlags._from_value(sent_payload(state)['flags']).voice

    with pytest.raises(TypeError):
        await text_channel().send('text', file=discord.File(io.BytesIO(b'a'), filename='v.ogg', duration=1.0, waveform=[1]))
    with pytest.raises(ValueError):
        discord.File(io.BytesIO(b'a'), filename='v.ogg', duration=1.0, waveform=[256])
    assert not discord.File(io.BytesIO(b'a'), filename='plain.txt').voice


# PR #10493: per-server avatar decorations


@requires(10493)
def test_guild_avatar_decoration():
    state = fake_state()
    data = {
        'user': {'id': '5', 'username': 'u', 'discriminator': '0', 'avatar': None},
        'roles': [],
        'joined_at': None,
        'flags': 0,
        'avatar_decoration_data': {'asset': 'a_abc', 'sku_id': '123'},
    }
    member = discord.Member(data=data, guild=fake_guild(state), state=state)  # pyright: ignore[reportArgumentType]
    assert member.guild_avatar_decoration_sku_id == 123
    assert member.guild_avatar_decoration is not None and 'a_abc' in member.guild_avatar_decoration.url
    assert member.display_avatar_decoration == member.guild_avatar_decoration


# PR #10442: last_pin_timestamp


@requires(10442)
def test_last_pin_timestamp_is_aware_utc():
    channel = text_channel(last_pin_timestamp='2026-01-02T03:04:05+00:00')
    assert channel.last_pin_timestamp == datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    assert text_channel().last_pin_timestamp is None


# PR #10428: shared client themes


@requires(10428)
@pytest.mark.asyncio
async def test_shared_client_theme_round_trip_and_send():
    theme = discord.SharedClientTheme(colours=[0xFF0000, 0x00FF00], gradient_angle=90, intensity=50)
    data = theme.to_dict()
    again = discord.SharedClientTheme.from_dict(data)
    assert [c.value for c in again.colours] == [0xFF0000, 0x00FF00] and again.gradient_angle == 90
    assert again.theme is theme.theme

    state = fake_state()
    await text_channel(state).send('hi', shared_client_theme=theme)
    assert sent_payload(state)['shared_client_theme'] == data


# PR #10514: spoiler channels


@requires(10514)
def test_spoiler_channels():
    assert discord.ChannelFlags._from_value(1 << 21).spoiler
    assert text_channel(flags=1 << 21).is_spoiler()
    assert not text_channel(flags=0).is_spoiler()


# PR #10386: community invites


@requires(10386)
@pytest.mark.asyncio
async def test_create_invite_with_roles_and_target_users(monkeypatch):
    state = fake_state()
    state.http.create_invite = AsyncMock(return_value={'code': 'abc'})
    monkeypatch.setattr(discord.Invite, 'from_incomplete', classmethod(lambda cls, **kwargs: 'invite'))
    channel = text_channel(state)
    await channel.create_invite(roles=[discord.Object(id=1)], target_users=[discord.Object(id=2)])
    kwargs = state.http.create_invite.await_args.kwargs
    assert kwargs['role_ids'] == [1] and kwargs['target_user_ids'] == [2]

    with pytest.raises(ValueError):
        await channel.create_invite(target_users=[discord.Object(id=2)], target_users_file=discord.File(io.BytesIO(b'2')))
    for method in ('target_users', 'add_target_user', 'remove_target_user', 'edit_target_users', 'target_users_job_status'):
        assert callable(getattr(discord.Invite, method))


# PR #10547 (+ fix patch): channel and role reorder audit log actions


@requires(10547)
@pytest.mark.parametrize('action_type, name', [(16, 'channel_position_update'), (33, 'role_position_update')])
def test_reorder_audit_log_entries(action_type, name):
    guild = MagicMock()
    guild.id, guild.get_member.return_value, guild.get_channel.return_value, guild.get_role.return_value = 1, None, None, None
    data = {'id': '1', 'user_id': '2', 'target_id': None, 'action_type': action_type, 'options': {'count': '3'}}
    entry = AuditLogEntry(
        users={}, integrations={}, app_commands={}, automod_rules={}, webhooks={}, data=data, guild=guild  # pyright: ignore[reportArgumentType]
    )
    assert entry.action is getattr(discord.AuditLogAction, name)
    assert entry.category is discord.AuditLogActionCategory.update
    assert entry.target is None  # used to raise TypeError
    assert entry.extra.count == 3  # pyright: ignore[reportAttributeAccessIssue]


def test_manifest_lists_every_configured_pr():
    if INCLUDED is None:
        pytest.skip('not a built branch')
    config = json.loads((MANIFEST.parent / 'prs.json').read_text())
    built = json.loads(MANIFEST.read_text())
    accounted = INCLUDED | {pr['number'] for pr in built['skipped']}
    assert accounted == {pr['number'] for pr in config['prs']}
