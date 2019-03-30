# Future patches
from __future__ import annotations

# Stdlib
from traceback import format_exc
from typing import TYPE_CHECKING, Any, Tuple
from uuid import UUID

# External Libraries
from anyio import sleep, create_event, create_task_group
from anyio.exceptions import TLSRequired
from quarry.data import packets
from quarry.net.crypto import Cipher, make_server_id, make_verify_token

# MCServer
from mcserver.classes.packet_decoder import PacketDecoder, IncompletePacket
from mcserver.classes.packet_encoder import PacketEncoder
from mcserver.objects.event_handler import EventHandler
from mcserver.objects.player_registry import PlayerRegistry
from mcserver.utils.logger import warn, debug, error

if TYPE_CHECKING:
    from typing import List, Dict, Union, Optional
    from anyio import SocketStream, Event
    from mcserver.events.event_base import Event as MCEvent
    from mcserver.classes.player import Player


class ClientConnection:
    def __init__(self, client: SocketStream):
        # TODO:
        # Refactor this class properties to delegate as much as possible to a different class
        # This class should only handle incoming/outgoing data and triggering events
        self.client = client
        self.do_loop = True
        self.protocol_state = "init"
        self.protocol_version = packets.default_protocol_version
        self.messages: List[bytes] = []
        self._locks: List[
            Dict[str,
                 Union[
                     str,
                     Event,
                     Optional[MCEvent]
                 ]]
        ] = []
        self.server_id = make_server_id()
        self.verify_token = make_verify_token()
        self.cipher = Cipher()

        self.name = ""
        self.uuid: UUID = None

    @property
    def player(self) -> Player:
        return PlayerRegistry.get_player(self.uuid)

    @property
    def packet_decoder(self):
        # TODO: Implement class
        return PacketDecoder(self.protocol_version)

    @property
    def packet_encoder(self):
        # TODO: Implement class
        return PacketEncoder(self.protocol_version)

    def __repr__(self):
        return (f"ClientConnection(loop={self.do_loop}, "
                f"message_queue={len(self.messages)}, "
                f"lock_queue={len(self._locks)})")

    async def serve(self):
        async with create_task_group() as tg:
            await tg.spawn(self.serve_loop)
            await tg.spawn(self.write_loop)

    async def serve_loop(self):
        data = b""
        run_again = False
        async with create_task_group() as tg:
            while self.do_loop:
                if not run_again:
                    try:
                        line = await self.client.receive_some(1024)
                    except ConnectionError:
                        line = b""

                    if line == b"":
                        try:
                            warn(f"Closing connection to {self.client.server_hostname}")
                        except TLSRequired:
                            pass

                        self.do_loop = False
                        break

                    data += self.cipher.decrypt(line)

                try:
                    rest_bytes, event = self.packet_decoder.decode(data)
                except IncompletePacket:
                    run_again = False
                    continue
                else:
                    data = rest_bytes
                    if data != b"":
                        run_again = True

                for lock in self._locks:
                    if lock["name"] == event.event:
                        self._locks.remove(lock)
                        lock["result"] = event
                        await lock["lock"].set()
                        break

                if event.event == "handshake":
                    await self.handle_msg(event)
                else:
                    tg.spawn(self.handle_msg, event)

            for lock in self._locks:
                await lock["lock"].set()
            if self.protocol_state == "play":
                # User was logged in
                debug("Player left, removing from game...")
                await EventHandler.handle_event(MCEvent("player_leave", self.player))  # TODO: Use PlayerLeaveEvent
                PlayerRegistry.players.remove(self.player)

    async def handle_msg(self, event: MCEvent):
        try:
            await EventHandler.handle_event(event)
        except Exception:  # pylint: disable=broad-except
            error(f"Exception occurred:\n{format_exc()}")

    async def write_loop(self):
        while self.do_loop:
            if self.messages:
                msg = self.messages.pop(0)
                debug(f"Sending to client: {msg}")
                await self.client.send_all(msg)
            else:
                await sleep(0.00001)  # Allow other tasks to run

    async def wait_for_packet(self, packet_name: str) -> Event:
        lock = {
            "name": packet_name,
            "lock": create_event(),
            "result": None
        }

        self._locks.append(lock)
        await lock["lock"].wait()

        return lock["result"]

    def send_packet(self, packet: bytes):
        self.messages.append(self.cipher.encrypt(packet))
