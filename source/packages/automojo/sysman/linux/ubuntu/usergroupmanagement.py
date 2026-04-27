"""
    Ubuntu user-group-management feature.

    Implements the cross-OS user-group-management contract for Ubuntu using
    ``useradd`` / ``usermod`` / ``userdel`` / ``groupadd`` / ``groupdel`` /
    ``gpasswd`` / ``passwd`` / ``chpasswd`` / ``getent`` / ``id`` through
    the injected :class:`ICommandRunner`.

    File I/O policy:
        Membership and account-state probes go through the runner via
        ``getent`` and ``passwd -S``.  The module does NOT consume an
        :class:`IFileOperator`; the established Ubuntu pattern routes
        every read / write through the runner so dry-run, elevation, and
        aspect composition compose uniformly.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_user``, ``get_group``, ``list_users``, ``list_groups``,
        idempotency probes) never elevate.

    Password handling:
        ``set_password`` pipes ``"<user>:<password>\\n"`` to ``chpasswd``
        via the runner's ``stdin`` channel.  The result's ``command``
        field is recorded as ``["chpasswd"]`` only — the password never
        leaks into the captured argv.
"""

import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_USERADD = "useradd"
_USERMOD = "usermod"
_USERDEL = "userdel"
_GROUPADD = "groupadd"
_GROUPDEL = "groupdel"
_GPASSWD = "gpasswd"
_PASSWD = "passwd"
_CHPASSWD = "chpasswd"
_GETENT = "getent"
_ID = "id"


UbuntuUserGroupActionStatus = ActionStatus


class UbuntuPrincipalKind(Enum):
    """
        Principal kind echoed onto every
        :class:`UbuntuUserGroupActionResult`.
    """
    USER = "user"
    GROUP = "group"


@dataclass
class UbuntuUserGroupManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuUserGroupManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param regular_uid_min: Lower bound (inclusive) for the UID range
                                used by :meth:`UbuntuUserGroupManager.list_users`
                                when filtering out system accounts.
        :param regular_uid_max: Upper bound (inclusive) for the UID range
                                used by :meth:`UbuntuUserGroupManager.list_users`
                                when filtering out system accounts.
        :param include_system_accounts: When True,
                                        :meth:`UbuntuUserGroupManager.list_users`
                                        returns every parsed entry without
                                        filtering by UID range.
    """
    regular_uid_min: int = 1000
    regular_uid_max: int = 60000
    include_system_accounts: bool = False


@dataclass
class UbuntuUserGroupActionResult(FeatureActionResult):
    """
        Result of a user-or-group write operation.

        :param name: The user or group name targeted.  ``None`` for
                     verbs that are not name-scoped.
        :param kind: Whether the principal is a user or a group.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    kind: Optional[UbuntuPrincipalKind] = None
    message: Optional[str] = None


@dataclass
class UbuntuUserSpec:
    """
        Specification for creating a user via ``useradd``.

        :param name: Login name.
        :param uid: Optional numeric UID (``-u``).  System allocates
                    one when None.
        :param primary_group: Optional primary group name (``-g``).
        :param shell: Login shell (``-s``).  Defaults to ``/bin/bash``.
        :param home: Optional home directory path (``-d``).
        :param comment: Optional GECOS / comment (``-c``).
        :param groups: Optional supplementary groups (``-G``,
                       comma-separated).
        :param system: True to create a system account (``-r``).
        :param create_home: True to pass ``-m`` so a home directory is
                            created.  Ignored when ``system`` is True
                            (system accounts skip ``-m`` by default).
    """
    name: str
    uid: Optional[int] = None
    primary_group: Optional[str] = None
    shell: str = "/bin/bash"
    home: Optional[str] = None
    comment: Optional[str] = None
    groups: Optional[list[str]] = None
    system: bool = False
    create_home: bool = True


@dataclass
class UbuntuUserChanges:
    """
        Specification for modifying an existing user account via
        ``usermod``.  Every field is optional; only fields that are not
        ``None`` are emitted as ``usermod`` flags.

        :param new_name: New login name (``-l``).
        :param uid: New numeric UID (``-u``).
        :param primary_group: New primary group (``-g``).
        :param home: New home directory (``-d``).
        :param shell: New login shell (``-s``).
        :param comment: New GECOS / comment (``-c``).
        :param groups: Supplementary group set (``-G``).
        :param append_groups: When True with ``groups`` set, ``-aG``
                              is emitted (additive).  Otherwise the
                              groups list replaces the user's
                              supplementary group set.
    """
    new_name: Optional[str] = None
    uid: Optional[int] = None
    primary_group: Optional[str] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    comment: Optional[str] = None
    groups: Optional[list[str]] = None
    append_groups: bool = False


@dataclass
class UbuntuUserInfo:
    """
        Information about an existing local user.

        :param name: Login name.
        :param uid: Numeric UID.
        :param gid: Numeric primary GID.
        :param home: Home directory.
        :param shell: Login shell.
        :param comment: GECOS / comment.
        :param groups: Names of every group the user belongs to
                       (primary plus supplementary), in the order
                       returned by ``id -Gn``.
        :param locked: True when ``passwd -S`` reports the account as
                       locked.
    """
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    comment: str
    groups: list[str] = field(default_factory=list)
    locked: bool = False


@dataclass
class UbuntuGroupSpec:
    """
        Specification for creating a group via ``groupadd``.

        :param name: Group name.
        :param gid: Optional numeric GID (``-g``).
        :param system: True to create a system group (``-r``).
    """
    name: str
    gid: Optional[int] = None
    system: bool = False


@dataclass
class UbuntuGroupInfo:
    """
        Information about an existing local group.

        :param name: Group name.
        :param gid: Numeric GID.
        :param members: Supplementary members of the group.
    """
    name: str
    gid: int
    members: list[str] = field(default_factory=list)


class UbuntuUserGroupQueryError(Exception):
    """
        Raised when a user / group read fails for a reason other than
        the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["User/group query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuUserGroupManager(BaseFeatureManager):
    """
        Ubuntu user-and-group manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`; reads use ``getent``,
        ``id``, and ``passwd -S`` while writes use ``useradd`` /
        ``usermod`` / ``userdel`` / ``groupadd`` / ``groupdel`` /
        ``gpasswd`` / ``passwd`` / ``chpasswd``.
    """

    result_class = UbuntuUserGroupActionResult
    status_class = UbuntuUserGroupActionStatus
    feature_name = "user-group-management"

    # ------------------------------------------------------------------
    # User write verbs
    # ------------------------------------------------------------------

    def create_user(
        self,
        spec: UbuntuUserSpec,
    ) -> UbuntuUserGroupActionResult:
        """
            Create a local user.  Skipped when the user already exists.

            :param spec: The :class:`UbuntuUserSpec` describing the
                         user to create.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = self._build_useradd_command(spec=spec)

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=spec.name,
                command=command,
            )
            return dry

        existing = self._lookup_passwd(name=spec.name)
        if existing is not None:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=spec.name,
                command=command,
                message="user already exists",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=spec.name)
        return rtnval

    def delete_user(
        self,
        name: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Delete a local user and its home directory via
            ``userdel -r``.  Skipped when the user does not exist.

            :param name: The login name to delete.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = [_USERDEL, "-r", name]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        existing = self._lookup_passwd(name=name)
        if existing is None:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=name,
                command=command,
                message="user does not exist",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=name)
        return rtnval

    def modify_user(
        self,
        name: str,
        changes: UbuntuUserChanges,
    ) -> UbuntuUserGroupActionResult:
        """
            Modify an existing user via ``usermod``.  Skipped when the
            current state already matches every requested change.

            :param name: The login name to modify.
            :param changes: The :class:`UbuntuUserChanges` describing
                            the desired new state.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = self._build_usermod_command(name=name, changes=changes)

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        existing = self.get_user(name=name)
        if existing is None:
            failed = self._build_user_result(
                status=UbuntuUserGroupActionStatus.FAILED,
                name=name,
                command=command,
                message="user does not exist",
                stderr="user does not exist",
            )
            return failed

        already_matches = _changes_match_current(
            current=existing,
            changes=changes,
        )
        if already_matches:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=name,
                command=command,
                message="user already matches requested state",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=name)
        return rtnval

    def set_password(
        self,
        user: str,
        password: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Set ``user``'s password via ``chpasswd``.  The password is
            piped through stdin so it never appears in argv.  No
            idempotency guard — the current hash cannot be read without
            elevated access to ``/etc/shadow``.

            :param user: The login name whose password to set.
            :param password: The plaintext password to set.

            :returns: The :class:`UbuntuUserGroupActionResult`.  The
                      ``command`` field is recorded as ``[_CHPASSWD]``
                      only; the password is never captured.
        """
        recorded_command = [_CHPASSWD]
        payload = "{}:{}\n".format(user, password)

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=user,
                command=recorded_command,
            )
            return dry

        run_result = self._runner.run(
            list(recorded_command),
            elevated=self._config.elevated,
            stdin=payload,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuUserGroupActionStatus.SUCCESS
        else:
            mapped_status = UbuntuUserGroupActionStatus.FAILED

        rtnval = UbuntuUserGroupActionResult(
            status=mapped_status,
            detail=user,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            name=user,
            kind=UbuntuPrincipalKind.USER,
        )
        return rtnval

    def lock_account(
        self,
        user: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Lock ``user``'s account via ``passwd -l``.  Skipped when the
            account is already locked.

            :param user: The login name to lock.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = [_PASSWD, "-l", user]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        is_locked = self._is_account_locked(user=user)
        if is_locked:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="account is already locked",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    def unlock_account(
        self,
        user: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Unlock ``user``'s account via ``passwd -u``.  Skipped when
            the account is not currently locked.

            :param user: The login name to unlock.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = [_PASSWD, "-u", user]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        is_locked = self._is_account_locked(user=user)
        if not is_locked:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="account is not locked",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    # ------------------------------------------------------------------
    # Group write verbs
    # ------------------------------------------------------------------

    def create_group(
        self,
        spec: UbuntuGroupSpec,
    ) -> UbuntuUserGroupActionResult:
        """
            Create a local group.  Skipped when the group already exists.

            :param spec: The :class:`UbuntuGroupSpec` describing the
                         group to create.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = self._build_groupadd_command(spec=spec)

        if self._config.dry_run:
            dry = self._build_group_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=spec.name,
                command=command,
            )
            return dry

        existing = self._lookup_group(name=spec.name)
        if existing is not None:
            skipped = self._build_group_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=spec.name,
                command=command,
                message="group already exists",
            )
            return skipped

        rtnval = self._dispatch_group_write(command=command, name=spec.name)
        return rtnval

    def delete_group(
        self,
        name: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Delete a local group via ``groupdel``.  Skipped when the
            group does not exist.

            :param name: The group name to delete.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = [_GROUPDEL, name]

        if self._config.dry_run:
            dry = self._build_group_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        existing = self._lookup_group(name=name)
        if existing is None:
            skipped = self._build_group_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=name,
                command=command,
                message="group does not exist",
            )
            return skipped

        rtnval = self._dispatch_group_write(command=command, name=name)
        return rtnval

    def add_to_group(
        self,
        user: str,
        group: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Add ``user`` to ``group`` as a supplementary member via
            ``gpasswd -a``.  Skipped when the user is already a member.

            :param user: The login name to add.
            :param group: The group name to add to.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = [_GPASSWD, "-a", user, group]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        group_info = self._lookup_group(name=group)
        if group_info is None:
            failed = self._build_user_result(
                status=UbuntuUserGroupActionStatus.FAILED,
                name=user,
                command=command,
                message="group does not exist",
                stderr="group does not exist",
            )
            return failed

        if user in group_info.members:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="user is already a member of the group",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    def remove_from_group(
        self,
        user: str,
        group: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Remove ``user`` from ``group`` via ``gpasswd -d``.  Skipped
            when the user is not a member.

            :param user: The login name to remove.
            :param group: The group name to remove from.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        command = [_GPASSWD, "-d", user, group]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=UbuntuUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        group_info = self._lookup_group(name=group)
        if group_info is None:
            failed = self._build_user_result(
                status=UbuntuUserGroupActionStatus.FAILED,
                name=user,
                command=command,
                message="group does not exist",
                stderr="group does not exist",
            )
            return failed

        if user not in group_info.members:
            skipped = self._build_user_result(
                status=UbuntuUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="user is not a member of the group",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_user(
        self,
        name: str,
    ) -> Optional[UbuntuUserInfo]:
        """
            Look up a user by name.

            Runs ``getent passwd <name>``, ``id -Gn <name>``, and
            ``passwd -S <name>``.  Reads do not elevate.

            :param name: The login name to look up.

            :returns: The :class:`UbuntuUserInfo`, or ``None`` when no
                      such user exists.
        """
        passwd_entry = self._lookup_passwd(name=name)
        if passwd_entry is None:
            rtnval: Optional[UbuntuUserInfo] = None
            return rtnval

        groups = self._lookup_user_groups(name=name)
        locked = self._is_account_locked(user=name)

        rtnval = UbuntuUserInfo(
            name=passwd_entry["name"],
            uid=passwd_entry["uid"],
            gid=passwd_entry["gid"],
            home=passwd_entry["home"],
            shell=passwd_entry["shell"],
            comment=passwd_entry["comment"],
            groups=groups,
            locked=locked,
        )
        return rtnval

    def list_users(self) -> list[UbuntuUserInfo]:
        """
            Enumerate every local user.

            Runs ``getent passwd`` and parses each line.  When
            :attr:`UbuntuUserGroupManagerConfig.include_system_accounts`
            is False, results are filtered to entries whose UID lies
            within the configured ``regular_uid_min`` /
            ``regular_uid_max`` inclusive range.  Reads do not elevate.

            :returns: One :class:`UbuntuUserInfo` per filtered entry.
            :raises UbuntuUserGroupQueryError: When ``getent passwd``
                                                fails with a non-zero
                                                exit code.
        """
        command = [_GETENT, "passwd"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuUserGroupQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        config = self._config
        if not isinstance(config, UbuntuUserGroupManagerConfig):
            uid_min = 1000
            uid_max = 60000
            include_system = False
        else:
            uid_min = config.regular_uid_min
            uid_max = config.regular_uid_max
            include_system = config.include_system_accounts

        rtnval: list[UbuntuUserInfo] = []
        for raw_line in run_result.stdout.splitlines():
            entry = _parse_getent_passwd_line(line=raw_line)
            if entry is None:
                continue
            if not include_system:
                entry_uid = entry["uid"]
                if entry_uid < uid_min:
                    continue
                if entry_uid > uid_max:
                    continue

            info = UbuntuUserInfo(
                name=entry["name"],
                uid=entry["uid"],
                gid=entry["gid"],
                home=entry["home"],
                shell=entry["shell"],
                comment=entry["comment"],
                groups=[],
                locked=False,
            )
            rtnval.append(info)

        return rtnval

    def get_group(
        self,
        name: str,
    ) -> Optional[UbuntuGroupInfo]:
        """
            Look up a group by name.

            :param name: The group name to look up.

            :returns: The :class:`UbuntuGroupInfo`, or ``None`` when no
                      such group exists.
        """
        rtnval = self._lookup_group(name=name)
        return rtnval

    def list_groups(self) -> list[UbuntuGroupInfo]:
        """
            Enumerate every local group.

            Runs ``getent group`` and parses each line.  Reads do not
            elevate.

            :returns: One :class:`UbuntuGroupInfo` per parsed line.
            :raises UbuntuUserGroupQueryError: When ``getent group``
                                                fails with a non-zero
                                                exit code.
        """
        command = [_GETENT, "group"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuUserGroupQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval: list[UbuntuGroupInfo] = []
        for raw_line in run_result.stdout.splitlines():
            info = _parse_getent_group_line(line=raw_line)
            if info is None:
                continue
            rtnval.append(info)

        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_useradd_command(
        self,
        *,
        spec: UbuntuUserSpec,
    ) -> list[str]:
        """
            Build the ``useradd`` argv from a :class:`UbuntuUserSpec`.

            :param spec: The :class:`UbuntuUserSpec` to render.

            :returns: A new list with the full argv.
        """
        command: list[str] = [_USERADD]

        if spec.system:
            command.append("-r")
        else:
            if spec.create_home:
                command.append("-m")

        if spec.uid is not None:
            command.append("-u")
            command.append(str(spec.uid))

        if spec.primary_group is not None:
            command.append("-g")
            command.append(spec.primary_group)

        if spec.groups is not None and len(spec.groups) > 0:
            command.append("-G")
            command.append(",".join(spec.groups))

        if spec.shell is not None:
            command.append("-s")
            command.append(spec.shell)

        if spec.home is not None:
            command.append("-d")
            command.append(spec.home)

        if spec.comment is not None:
            command.append("-c")
            command.append(spec.comment)

        command.append(spec.name)
        return command

    def _build_usermod_command(
        self,
        *,
        name: str,
        changes: UbuntuUserChanges,
    ) -> list[str]:
        """
            Build the ``usermod`` argv from a :class:`UbuntuUserChanges`.

            :param name: The current login name.
            :param changes: The desired changes.

            :returns: A new list with the full argv.
        """
        command: list[str] = [_USERMOD]

        if changes.new_name is not None:
            command.append("-l")
            command.append(changes.new_name)

        if changes.uid is not None:
            command.append("-u")
            command.append(str(changes.uid))

        if changes.primary_group is not None:
            command.append("-g")
            command.append(changes.primary_group)

        if changes.home is not None:
            command.append("-d")
            command.append(changes.home)

        if changes.shell is not None:
            command.append("-s")
            command.append(changes.shell)

        if changes.comment is not None:
            command.append("-c")
            command.append(changes.comment)

        if changes.groups is not None:
            joined = ",".join(changes.groups)
            if changes.append_groups:
                command.append("-aG")
                command.append(joined)
            else:
                command.append("-G")
                command.append(joined)

        command.append(name)
        return command

    def _build_groupadd_command(
        self,
        *,
        spec: UbuntuGroupSpec,
    ) -> list[str]:
        """
            Build the ``groupadd`` argv from a :class:`UbuntuGroupSpec`.

            :param spec: The :class:`UbuntuGroupSpec` to render.

            :returns: A new list with the full argv.
        """
        command: list[str] = [_GROUPADD]
        if spec.system:
            command.append("-r")
        if spec.gid is not None:
            command.append("-g")
            command.append(str(spec.gid))
        command.append(spec.name)
        return command

    def _dispatch_user_write(
        self,
        *,
        command: list[str],
        name: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Dispatch a user-scope write command and wrap its outcome.

            :param command: The full argv.
            :param name: The login name targeted.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="user_write",
            name=name,
            kind=UbuntuPrincipalKind.USER,
        )
        return rtnval

    def _dispatch_group_write(
        self,
        *,
        command: list[str],
        name: str,
    ) -> UbuntuUserGroupActionResult:
        """
            Dispatch a group-scope write command and wrap its outcome.

            :param command: The full argv.
            :param name: The group name targeted.

            :returns: The :class:`UbuntuUserGroupActionResult`.
        """
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="group_write",
            name=name,
            kind=UbuntuPrincipalKind.GROUP,
        )
        return rtnval

    def _build_user_result(
        self,
        *,
        status: UbuntuUserGroupActionStatus,
        name: str,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuUserGroupActionResult:
        """
            Construct a user-kind :class:`UbuntuUserGroupActionResult`
            without invoking the runner.
        """
        rtnval = UbuntuUserGroupActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            kind=UbuntuPrincipalKind.USER,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _build_group_result(
        self,
        *,
        status: UbuntuUserGroupActionStatus,
        name: str,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuUserGroupActionResult:
        """
            Construct a group-kind :class:`UbuntuUserGroupActionResult`
            without invoking the runner.
        """
        rtnval = UbuntuUserGroupActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            kind=UbuntuPrincipalKind.GROUP,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _lookup_passwd(self, *, name: str) -> Optional[dict]:
        """
            Run ``getent passwd <name>`` and parse the result.

            :param name: The login name to look up.

            :returns: A dict with parsed fields, or ``None`` when the
                      user does not exist (``getent`` returns 2).
        """
        command = [_GETENT, "passwd", name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_getent_passwd_line(line=run_result.stdout)
        return rtnval

    def _lookup_group(self, *, name: str) -> Optional[UbuntuGroupInfo]:
        """
            Run ``getent group <name>`` and parse the result.

            :param name: The group name to look up.

            :returns: The :class:`UbuntuGroupInfo`, or ``None`` when
                      the group does not exist.
        """
        command = [_GETENT, "group", name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[UbuntuGroupInfo] = None
            return rtnval
        rtnval = _parse_getent_group_line(line=run_result.stdout)
        return rtnval

    def _lookup_user_groups(self, *, name: str) -> list[str]:
        """
            Run ``id -Gn <name>`` and split the result.

            :param name: The login name to look up.

            :returns: The list of group names; empty on failure.
        """
        command = [_ID, "-Gn", name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list[str] = []
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = []
            return rtnval

        rtnval = stripped.split()
        return rtnval

    def _is_account_locked(self, *, user: str) -> bool:
        """
            Run ``passwd -S <user>`` and inspect the second whitespace
            field.

            :param user: The login name to probe.

            :returns: True when ``passwd -S`` reports an ``L`` flag in
                      the second field; False otherwise.  Reads never
                      elevate; on a non-zero exit ``False`` is returned
                      so the caller decides how to surface absence.
        """
        command = [_PASSWD, "-S", user]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        parts = run_result.stdout.split()
        if len(parts) < 2:
            rtnval = False
            return rtnval

        flag = parts[1]
        rtnval = flag == "L"
        return rtnval


def _parse_getent_passwd_line(*, line: str) -> Optional[dict]:
    """
        Parse a single ``getent passwd`` line into a dict.

        Expected colon-separated form:
            ``<name>:<x>:<uid>:<gid>:<comment>:<home>:<shell>``.

        :param line: A raw line of stdout.  May contain trailing
                     newline / multiple newlines (only the first
                     non-empty line is parsed).

        :returns: The parsed dict, or ``None`` when the line cannot be
                  parsed.
    """
    rtnval: Optional[dict] = None
    for raw in line.splitlines():
        stripped = raw.rstrip("\n").rstrip("\r")
        if len(stripped) == 0:
            continue

        fields = stripped.split(":")
        if len(fields) < 7:
            continue

        try:
            uid_value = int(fields[2])
        except ValueError:
            continue

        try:
            gid_value = int(fields[3])
        except ValueError:
            continue

        rtnval = {
            "name": fields[0],
            "uid": uid_value,
            "gid": gid_value,
            "comment": fields[4],
            "home": fields[5],
            "shell": fields[6],
        }
        break

    return rtnval


def _parse_getent_group_line(*, line: str) -> Optional[UbuntuGroupInfo]:
    """
        Parse a single ``getent group`` line into a
        :class:`UbuntuGroupInfo`.

        Expected colon-separated form:
            ``<name>:<x>:<gid>:<member1>,<member2>,...``.

        :param line: A raw line of stdout.

        :returns: The parsed info, or ``None`` when the line cannot be
                  parsed.
    """
    rtnval: Optional[UbuntuGroupInfo] = None
    for raw in line.splitlines():
        stripped = raw.rstrip("\n").rstrip("\r")
        if len(stripped) == 0:
            continue

        fields = stripped.split(":")
        if len(fields) < 4:
            continue

        try:
            gid_value = int(fields[2])
        except ValueError:
            continue

        member_field = fields[3]
        members: list[str] = []
        if len(member_field) > 0:
            for token in member_field.split(","):
                cleaned = token.strip()
                if len(cleaned) > 0:
                    members.append(cleaned)

        rtnval = UbuntuGroupInfo(
            name=fields[0],
            gid=gid_value,
            members=members,
        )
        break

    return rtnval


def _changes_match_current(
    *,
    current: UbuntuUserInfo,
    changes: UbuntuUserChanges,
) -> bool:
    """
        Return True when every non-``None`` field on ``changes``
        already matches ``current``.

        :param current: The user's currently-observed info.
        :param changes: The requested changes.

        :returns: True when no ``usermod`` invocation would alter the
                  user; False otherwise.
    """
    if changes.new_name is not None:
        if changes.new_name != current.name:
            rtnval = False
            return rtnval

    if changes.uid is not None:
        if changes.uid != current.uid:
            rtnval = False
            return rtnval

    if changes.home is not None:
        if changes.home != current.home:
            rtnval = False
            return rtnval

    if changes.shell is not None:
        if changes.shell != current.shell:
            rtnval = False
            return rtnval

    if changes.comment is not None:
        if changes.comment != current.comment:
            rtnval = False
            return rtnval

    # Primary group is reported as gid in current; matching by name
    # requires resolving the gid.  We compare lazily: when the caller
    # specifies a primary_group we cannot prove identity by integer
    # comparison alone, so a non-None primary_group always triggers a
    # write.  This keeps the idempotency guard conservative.
    if changes.primary_group is not None:
        rtnval = False
        return rtnval

    if changes.groups is not None:
        desired = set(changes.groups)
        observed = set(current.groups)
        if changes.append_groups:
            if not desired.issubset(observed):
                rtnval = False
                return rtnval
        else:
            if desired != observed:
                rtnval = False
                return rtnval

    rtnval = True
    return rtnval
