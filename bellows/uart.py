import asyncio
import binascii
import logging
import serial_asyncio
import serial

import bellows.types as t


LOGGER = logging.getLogger(__name__)


class Gateway(asyncio.Protocol):
    FLAG = b'\x7E'  # Marks end of frame
    ESCAPE = b'\x7D'
    XON = b'\x11'  # Resume transmission
    XOFF = b'\x13'  # Stop transmission
    SUBSTITUTE = b'\x18'
    CANCEL = b'\x1A'  # Terminates a frame in progress
    STUFF = 0x20
    RANDOMIZE_START = 0x42
    RANDOMIZE_SEQ = 0xB8
    reTx = 0b00001000
    ASH_TIMEOUT=2

    RESERVED = FLAG + ESCAPE + XON + XOFF + SUBSTITUTE + CANCEL

    class Terminator:
        pass

    def __init__(self, application, connected_future=None):
        self._send_seq = 0
        self._rec_seq = 0
        self._buffer = b''
        self._application = application
        self._reset_future = None
        self._connected_future = connected_future
        self._sendq = asyncio.Queue()
        self._pending = (-1, None)
        self._reject_mode = 0
        self._rx_buffer = dict()
        self._send_win = 4
        self._tx_buffer = dict()
        self._send_ack = 0
        self._failed_mode = 0
        self._Run_Event = asyncio.Event()
        self._Run_Event.set()
        self._task_send_task = None
        self._stats_frames_rx = 0
        self._stats_frames_rx_dup = 0
        self._stats_frames_tx = 0
        self._stats_frames_error = 0
        self._stats_frames_reset = 0

    def status(self):
        return [bool(self._failed_mode),
                self._task_send_task.done(),
                not self._Run_Event.is_set(),
                ]

    def stats(self):
        return {
                    "stats_frames_rx": self._stats_frames_rx,
                    "stats_frames_tx": self._stats_frames_tx,
                    "stats_frames_rx_dup": self._stats_frames_rx_dup,
                    "stats_frames_error": self._stats_frames_error,
                    "stats_frames_reset": self._stats_frames_reset,
                  }

    def connection_made(self, transport):
        """Callback when the uart is connected."""
        self._transport = transport
        if self._connected_future is not None:
            self._connected_future.set_result(True)
            self._task_send_task = asyncio.ensure_future(self._send_task())

    def data_received(self, data):
        """Callback when there is data received from the uart."""
        # TODO: Fix this handling for multiple instances of the characters
        # If a Cancel Byte or Substitute Byte is received, the bytes received
        # so far are discarded. In the case of a Substitute Byte, subsequent
        # bytes will also be discarded until the next Flag Byte.
        if self.CANCEL in data:
            self._buffer = b''
            data = data[data.rfind(self.CANCEL) + 1:]
        if self.SUBSTITUTE in data:
            self._buffer = b''
            data = data[data.find(self.FLAG) + 1:]

        self._buffer += data
        while self._buffer:
            frame, self._buffer = self._extract_frame(self._buffer)
            if frame is None:
                break
            self.frame_received(frame)

    def _extract_frame(self, data):
        """Extract a frame from the data buffer."""
        if self.FLAG in data:
            place = data.find(self.FLAG)
            return self._unstuff(data[:place + 1]), data[place + 1:]
        return None, data

    def frame_received(self, data):
        self._stats_frames_rx += 1
        LOGGER.debug("Status _send_task.done: %s", self._task_send_task.done())
        """Frame receive handler."""
        if (data[0] & 0b10000000) == 0:
            self.data_frame_received(data)
        elif (data[0] & 0b11100000) == 0b10000000:
            self.ack_frame_received(data)
        elif (data[0] & 0b11100000) == 0b10100000:
            self.nak_frame_received(data)
        elif data[0] == 0b11000000:
            self.rst_frame_received(data)
        elif data[0] == 0b11000001:
            self.rstack_frame_received(data)
        elif data[0] == 0b11000010:
            self.error_frame_received(data)
        else:
            LOGGER.error("UNKNOWN FRAME RECEIVED: %r", data)  # TODO

    def data_frame_received(self, data):
        """Data frame receive handler."""
        seq = (data[0] & 0b01110000) >> 4

        retrans = 1 if data[0] & self.reTx else 0
#        if data[0] & self.reTx:
#            retrans = 1
#        else:
#            retrans = 0
        LOGGER.debug("Data frame SEQ(%s)/ReTx(%s): %s", seq, retrans,  binascii.hexlify(data))
        if self._rec_seq != seq and not retrans:
            if not self._reject_mode:
                self._reject_mode = 1
                self.write(self._nack_frame())
                LOGGER.debug("Reject_mode on: expect SEQ(%s), got sEQ(%s)", self._rec_seq, seq)
            return
        else:
            if self._reject_mode:
                self._reject_mode = 0
                LOGGER.debug("Reject_mode off SEQ(%s)", seq)
            self._rec_seq = (seq + 1) % 8
#            if self._Run_Event.is_set():
            self.write(self._ack_frame())
            self._handle_ack(data[0])
            frame_data = self._randomize(data[1:-3])
            if retrans and (self._rx_buffer[seq] == frame_data):
                LOGGER.debug("DUP Data frame SEQ(%s)/ReTx(%s): %s", seq, retrans,  binascii.hexlify(data))
                self._stats_frames_rx_dup += 1
                return

            self._rx_buffer[seq] = frame_data
            try:
                self._application.frame_received(frame_data)
                LOGGER.debug("Process Data frame SEQ(%s)/ReTx(%s): %s", seq, retrans,  binascii.hexlify(frame_data))
            except:
                pass

    def ack_frame_received(self, data):
        """Acknowledgement frame receive handler."""
        LOGGER.debug("ACK frame: %s", binascii.hexlify(data))
        self._handle_ack(data[0])

    def nak_frame_received(self, data):
        """Negative acknowledgement frame receive handler."""
        LOGGER.debug("NAK frame: %s", binascii.hexlify(data))
        self._handle_nak(data[0])

    def rst_frame_received(self, data):
        """Reset frame handler."""
        LOGGER.debug("RST frame: %s", binascii.hexlify(data))

    def rstack_frame_received(self, data):
        """Reset acknowledgement frame receive handler."""
        self._send_seq = 0
        self._rec_seq = 0
        self._reject_mode = 0
        self._failed_mode = 0
        try:
            code = t.NcpResetCode(data[2])
        except ValueError:
            code = t.NcpResetCode.ERROR_UNKNOWN_EM3XX_ERROR

        LOGGER.debug("RSTACK Version: %d Reason: %s frame: %s", data[1], code.name, binascii.hexlify(data))
        # Only handle the frame, if it is a reply to our reset request
        if code is not t.NcpResetCode.RESET_SOFTWARE:
            return

        if self._reset_future is None:
            LOGGER.warn("Reset future is None")
            return

        self._reset_future.set_result(True)

    def error_frame_received(self, data):
        """Error frame receive handler."""
        self._stats_frames_error += 1
        try:
            code = t.NcpResetCode(data[2])
        except ValueError:
            code = t.NcpResetCode.ERROR_UNKNOWN_EM3XX_ERROR
        if code is t.NcpResetCode.ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT and self._Run_Event.is_set():
            LOGGER.error("Error (%s), reset connection", code.name)
            self._failed_mode = 1
            self._Run_Event.clear()
            pending, self._pending = self._pending, (-1, None)
            if pending[1]:
                pending[1].set_result(True)
            self._application.restart()
        else:
            LOGGER.debug("Error frame: %s", binascii.hexlify(data))

    def write(self, data):
        """Send data to the uart."""
        LOGGER.debug("Sending: %s", binascii.hexlify(data))
        self._transport.write(data)
        self._stats_frames_tx += 1
        

    def close(self):
        self._sendq.put_nowait(self.Terminator)
        self._transport.close()

    async def reset(self):
        """Sends a reset frame."""
        self._stats_frames_reset += 1
        LOGGER.debug("Sending: RESET")
        if self._reset_future is not None:
            if self._failed_mode == 1: # restart ongoing
                LOGGER.debug("cancel stale reset")
                self._reset_future.cancel()
                try: 
                    await self._reset_future
                except asyncio.CancelledError:
                    self._reset_future = None
            else:
                LOGGER.debug("reset can only be called on a new connection")
                return
            
        # Make sure the reset receives a rst_ack in time
        result = False
        while result == False:
            self._reset_future = asyncio.Future()
            self.write(self._rst_frame())
            try:
                result = await asyncio.wait_for(
                        self._reset_future, self.ASH_TIMEOUT
                    )
            except asyncio.TimeoutError:
                result = False
                LOGGER.error("Reset NCP failed")
            except asyncio.CancelledError:
                    self._reset_future = None
                
        self._reset_future = None
        LOGGER.debug("Reset success")

    async def _send_task(self):
        """Send queue handler."""
        LOGGER.debug("Start sendq loop")
        while True:
            await self._Run_Event.wait()
#            LOGGER.debug("read sendq item")
            item = await self._sendq.get()
            if item is self.Terminator:
                break
            await self.data_noqueue(item)
        LOGGER.debug("End sendq loop")

    async def data_noqueue(self, item):
        seq = self._send_seq
        self._send_seq = (seq + 1) % 8
        success = False
        rxmit = 0
        self._tx_buffer[seq] = item
        while not success:
            self._pending = (seq, asyncio.Future())
            self.write(self._data_frame(item, seq, rxmit))
            rxmit = 1
            try:
                success = await asyncio.wait_for(self._pending[1], 2)
            except asyncio.TimeoutError:
                success = True
                LOGGER.warn("Timeout for ASH send frame")
            except asyncio.CancelledError:
                success = True
                LOGGER.warn("future canceled for ASH send frame")

    def _handle_ack(self, control):
        """Handle an acknowledgement frame."""
        ack = ((control & 0b00000111) - 1) % 8
        if ack == self._pending[0]:
            pending, self._pending = self._pending, (-1, None)
            try:
                pending[1].set_result(True)
            except:
                pass

    def _handle_nak(self, control):
        """Handle negative acknowledgment frame."""
        nak = control & 0b00000111
        if nak == self._pending[0]:
            self._pending[1].set_result(False)

    def data(self, data):
        """Send a data frame."""
        self._sendq.put_nowait((data))
        LOGGER.debug("add command to send queue: %s-%s", self._task_send_task.done(), self._Run_Event.is_set())

    def _data_frame(self, data, seq, rxmit):
        """Construct a data frame."""
        assert 0 <= seq <= 7
        assert 0 <= rxmit <= 1
        control = (seq << 4) | (rxmit << 3) | self._rec_seq
        return self._frame(bytes([control]), self._randomize(data))

    def _ack_frame(self):
        """Construct a acknowledgement frame."""
        assert 0 <= self._rec_seq < 8
        control = bytes([0b10000000 | (self._rec_seq & 0b00000111)])
        return self._frame(control, b'')

    def _nack_frame(self):
        """Construct a nack frame."""
        control = bytes([0b10100000 | (self._rec_seq & 0b00000111)])
        return self._frame(control, b'')

    def _rst_frame(self):
        """Construct a reset frame."""
        return self.CANCEL + self._frame(b'\xC0', b'')

    def _frame(self, control, data):
        """Construct a frame."""
        crc = binascii.crc_hqx(control + data, 0xffff)
        crc = bytes([crc >> 8, crc % 256])
        return self._stuff(control + data + crc) + self.FLAG

    def _randomize(self, s):
        """XOR s with a pseudo-random sequence for transmission.

        Used only in data frames

        """
        rand = self.RANDOMIZE_START
        out = b''
        for c in s:
            out += bytes([c ^ rand])
            if rand % 2:
                rand = (rand >> 1) ^ self.RANDOMIZE_SEQ
            else:
                rand = rand >> 1
        return out

    def _stuff(self, s):
        """Byte stuff (escape) a string for transmission."""
        out = b''
        for c in s:
            if c in self.RESERVED:
                out += self.ESCAPE + bytes([c ^ self.STUFF])
            else:
                out += bytes([c])
        return out

    def _unstuff(self, s):
        """Unstuff (unescape) a string after receipt."""
        out = b''
        escaped = False
        for c in s:
            if escaped:
                out += bytes([c ^ self.STUFF])
                escaped = False
            elif c in self.ESCAPE:
                escaped = True
            else:
                out += bytes([c])
        return out

    def Run_enable(self):
        self._Run_Event.set()
        LOGGER.debug("enable run")


async def connect(port, baudrate, application, loop=None):
    if loop is None:
        loop = asyncio.get_event_loop()

    connection_future = asyncio.Future()
    protocol = Gateway(application, connection_future)

    transport, protocol = await serial_asyncio.create_serial_connection(
        loop,
        lambda: protocol,
        url=port,
        baudrate=baudrate,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        xonxoff=True,
    )

    await connection_future

    return protocol
