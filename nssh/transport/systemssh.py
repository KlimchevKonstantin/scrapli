"""nssh.transport.systemssh"""
import re
from logging import getLogger
from select import select
from subprocess import PIPE, Popen
from threading import Lock
from typing import TYPE_CHECKING, List, Optional, Union

from nssh.decorators import operation_timeout
from nssh.exceptions import NSSHAuthenticationFailed
from nssh.helper import get_prompt_pattern
from nssh.transport.ptyprocess import PtyProcess
from nssh.transport.transport import Transport

if TYPE_CHECKING:
    PopenBytes = Popen[bytes]  # pylint: disable=E1136
else:
    PopenBytes = Popen

LOG = getLogger("transport")

SYSTEM_SSH_TRANSPORT_ARGS = (
    "host",
    "port",
    "timeout_ssh",
    "auth_username",
    "auth_public_key",
    "auth_password",
    "comms_return_char",
)


class SystemSSHTransport(Transport):
    def __init__(
        self,
        host: str,
        port: int = 22,
        timeout_ssh: int = 5000,
        auth_username: str = "",
        auth_public_key: str = "",
        auth_password: str = "",
        comms_prompt_pattern: str = r"^[a-z0-9.\-@()/:]{1,32}[#>$]$",
        comms_return_char: str = "\n",
    ):  # pylint: disable=W0231
        """
        SystemSSHTransport Object

        Inherit from Transport ABC
        SSH2Transport <- Transport (ABC)

        Args:
            host: host ip/name to connect to
            port: port to connect to
            timeout_ssh: timeout for ssh2 transport in milliseconds
            auth_username: username for authentication
            auth_public_key: path to public key for authentication
            auth_password: password for authentication
            comms_prompt_pattern: prompt pattern expected for device, same as the one provided to
                channel -- system ssh needs to know this to know how to decide if we are properly
                sending/receiving data -- i.e. we are not stuck at some password prompt or some
                other failure scenario. If using driver, this should be passed from driver (NSSH, or
                IOSXE, etc.) to this Transport class.
            comms_return_char: return character to use on the channel, same as the one provided to
                channel -- system ssh needs to know this to know what to send so that we can probe
                the channel to make sure we are authenticated and sending/receiving data. If using
                driver, this should be passed from driver (NSSH, or IOSXE, etc.) to this Transport
                class.

        Returns:
            N/A  # noqa

        Raises:
            AuthenticationFailed: if all authentication means fail

        """
        self.host: str = host
        self.port: int = port
        self.timeout_ssh: int = timeout_ssh
        self.session_lock: Lock = Lock()
        self.auth_username: str = auth_username
        self.auth_public_key: str = auth_public_key
        self.auth_password: str = auth_password
        self.comms_prompt_pattern: str = comms_prompt_pattern
        self.comms_return_char: str = comms_return_char

        self.session: Union[Popen[bytes], PtyProcess]  # pylint: disable=E1136
        self.lib_auth_exception = NSSHAuthenticationFailed
        self._isauthenticated = False

    def open(self) -> None:
        """
        Parent method to open session, authenticate and acquire shell

        Args:
            N/A  # noqa

        Returns:
            N/A  # noqa

        Raises:
            AuthenticationFailed: if all authentication means fail

        """
        self.session_lock.acquire()
        # TODO -- construct open command -- this means parsing ssh keys and such prior to getting
        #  here in case users dont want to just rely on their ssh config file
        open_cmd = ["ssh", self.host, "-l", self.auth_username]

        # If authenticating with public key prefer to use open pipes
        # _open_pipes uses subprocess Popen which is preferable to opening a pty
        if self.auth_public_key:
            open_cmd.extend(["-i", self.auth_public_key])
            if self._open_pipes(open_cmd):
                return

        # If public key auth fails or is not configured, open a pty session
        if not self._open_pty(open_cmd):
            msg = f"Authentication to host {self.host} failed"
            LOG.critical(msg)
            raise NSSHAuthenticationFailed(msg)

    def _open_pipes(self, open_cmd: List[str]) -> bool:
        """
        Private method to open session with subprocess.Popen

        Args:
            open_cmd: ssh command string to use to open connection

        Returns:
            bool: True/False session was opened and authenticated

        Raises:
            N/A  # noqa

        """
        open_cmd.append("-v")
        pipes_session = Popen(
            open_cmd, bufsize=0, shell=False, stdin=PIPE, stdout=PIPE, stderr=PIPE
        )
        LOG.debug(f"Session to host {self.host} spawned")

        try:
            self._pipes_isauthenticated(pipes_session)
        except TimeoutError:
            return False

        LOG.debug(f"Authenticated to host {self.host} with public key")
        self.session_lock.release()
        self.session = pipes_session
        return True

    @operation_timeout("timeout_ssh")
    def _pipes_isauthenticated(self, pipes_session: PopenBytes) -> bool:
        """
        Private method to check initial authentication when using subprocess.Popen

        Args:
            pipes_session: Popen pipes session object

        Returns:
            bool: True/False session was authenticated

        Raises:
            N/A  # noqa

        """
        output = b""
        while True:
            output += pipes_session.stderr.read(1024)
            if f"Authenticated to {self.host}".encode() in output:
                self._isauthenticated = True
                return True

    def _open_pty(self, open_cmd: List[str]) -> bool:
        """
        Private method to open session with PtyProcess

        Args:
            open_cmd: ssh command string to use to open connection

        Returns:
            bool: True/False session was opened and authenticated

        Raises:
            N/A  # noqa

        """
        pty_session = PtyProcess.spawn(open_cmd)
        LOG.debug(f"Session to host {self.host} spawned")
        self.session_lock.release()
        self._pty_authenticate(pty_session)
        if not self._pty_isauthenticated(pty_session):
            return False
        LOG.debug(f"Authenticated to host {self.host} with password")
        self.session = pty_session
        return True

    def _pty_authenticate(self, pty_session: PtyProcess) -> None:
        """
        Private method to check initial authentication when using pty_session

        Args:
            pty_session: PtyProcess session object

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        self.session_lock.acquire()
        try:
            attempt_count = 0
            while True:
                output = pty_session.read()
                if b"password" in output.lower():
                    pty_session.write(self.auth_password.encode())
                    pty_session.write(b"\n")
                    break
                attempt_count += 1
                if attempt_count > 250:
                    raise self.lib_auth_exception
        except self.lib_auth_exception as exc:
            LOG.critical(f"Password authentication with host {self.host} failed. Exception: {exc}.")
        except Exception as exc:
            LOG.critical(
                "Unknown error occurred during password authentication with host "
                f"{self.host}; Exception: {exc}"
            )
            raise exc
        finally:
            self.session_lock.release()

    def _pty_isauthenticated(self, pty_session: PtyProcess) -> bool:
        """
        Check if session is authenticated

        This is very naive -- it only knows if the sub process is alive and has not received an EOF.
        Beyond that we lock the session and send the return character and re-read the channel.

        Args:
            N/A  # noqa

        Returns:
            authenticated: True if authenticated, else False

        Raises:
            N/A  # noqa

        """
        if pty_session.isalive() and not pty_session.eof():
            prompt_pattern = get_prompt_pattern("", self.comms_prompt_pattern)
            self.session_lock.acquire()
            pty_session.write(self.comms_return_char.encode())
            fd_ready, _, _ = select([pty_session.fd], [], [], 0)
            if pty_session.fd in fd_ready:
                # TODO -- it seems that i need two reads here...? doesn't seem to be a problem
                #  elsewhere though...? not sure whats going on
                pty_session.read()
                output = pty_session.read()
                # we do not need to deal w/ line replacement for the actual output, only for
                # parsing if a prompt-like thing is at the end of the output
                output = re.sub(b"\r", b"\n", output.strip())
                channel_match = re.search(prompt_pattern, output)
                if channel_match:
                    self.session_lock.release()
                    self._isauthenticated = True
                    return True
        self.session_lock.release()
        return False

    def close(self) -> None:
        """
        Close session and socket

        Args:
            N/A  # noqa

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        self.session_lock.acquire()
        if isinstance(self.session, Popen):
            self.session.kill()
        elif isinstance(self.session, PtyProcess):
            self.session.kill(1)
        LOG.debug(f"Channel to host {self.host} closed")
        self.session_lock.release()

    def isalive(self) -> bool:
        """
        Check if socket is alive and session is authenticated

        Args:
            N/A  # noqa

        Returns:
            bool: True if socket is alive and session authenticated, else False

        Raises:
            N/A  # noqa

        """
        if isinstance(self.session, Popen):
            if self.session.poll() is None and self._isauthenticated:
                return True
        elif isinstance(self.session, PtyProcess):
            if self.session.isalive() and self._isauthenticated and not self.session.eof():
                return True
        return False

    def read(self) -> bytes:
        """
        Read data from the channel

        Args:
            N/A  # noqa

        Returns:
            bytes_read: int of bytes read
            output: bytes output as read from channel

        Raises:
            N/A  # noqa

        """
        # TODO what value should read be...?
        read_bytes = 65535
        if isinstance(self.session, Popen):
            return self.session.stdout.read(read_bytes)
        if isinstance(self.session, PtyProcess):
            return self.session.read(read_bytes)
        return b""

    def write(self, channel_input: str) -> None:
        """
        Write data to the channel

        Args:
            channel_input: string to send to channel

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        if isinstance(self.session, Popen):
            self.session.stdin.write(channel_input.encode())
        elif isinstance(self.session, PtyProcess):
            self.session.write(channel_input.encode())

    def flush(self) -> None:
        """
        Flush channel stdout stream

        Args:
            N/A  # noqa

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        if isinstance(self.session, Popen):
            # TODO -- is this ok? should i read off the channel?
            pass
        elif isinstance(self.session, PtyProcess):
            self.session.flush()

    def set_timeout(self, timeout: Optional[int] = None) -> None:
        """
        Set session timeout

        Args:
            timeout: timeout in seconds

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
        # TODO would be good to be able to set this?? Or is it unnecessary for popen??

    def set_blocking(self, blocking: bool = False) -> None:
        """
        Set session blocking configuration

        Unnecessary when using Popen/system ssh

        Args:
            blocking: True/False set session to blocking

        Returns:
            N/A  # noqa

        Raises:
            N/A  # noqa

        """
