# Requirements

## Introduction

The user and group management feature provides the `automojo-sysman-linux-ubuntu`
package with the ability to manage local users and local groups on Ubuntu systems.  This
spec implements the user-group-management contract defined at
`automojo-sysman-common/specs/contracts/user-group-management/contract.md` for Ubuntu
using the shadow-utils tooling (`useradd`, `userdel`, `usermod`, `groupadd`, `groupdel`,
`gpasswd`, `passwd`, `chpasswd`) and the name-service query tool `getent`.  It includes
creating, deleting, modifying, and querying users and groups, managing group membership,
setting passwords, and locking and unlocking accounts.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *which* users or
groups should exist — it provides the capability to *manipulate them* reliably,
idempotently, and with full auditability.  All system commands are executed exclusively
through the `automojo-interfaces` command-runner abstraction, enabling dry-run
simulation, auditing, and test mocking without requiring a live system.  Passwords passed
to `set_password` are forwarded to `chpasswd` via stdin and never appear in the argv
captured on a `UbuntuUserGroupActionResult`.

---

## Requirements

### Requirement 1: Create a Local User

**User Story:** As a maintenance coordinator, I want to create a local user on an Ubuntu
system from a supplied `UbuntuUserSpec`, so that the account becomes available for
interactive or service use with the UID, shell, home directory, primary group, and
comment the spec requires.

#### Acceptance Criteria

1. WHEN the coordinator provides a `UbuntuUserSpec` THE SYSTEM SHALL construct a
   `useradd -m` invocation augmented with `-s <shell>`, `-u <uid>`, `-g <primary_group>`,
   and `-c <comment>` for every spec field that is not `None`, append the username as the
   final positional argument, and dispatch the argv through the command runner with
   `elevated=True` when configured for elevation.
2. WHEN `UbuntuUserSpec.system_account` is `True` THE SYSTEM SHALL add the `-r` flag to
   the `useradd` argv so the user is allocated a UID in the system range.
3. WHEN a user with the same name already exists (detected via `getent passwd <name>`)
   THE SYSTEM SHALL skip execution and return a `UbuntuUserGroupActionResult` with
   `status=SKIPPED` and `kind="user"`.
4. WHEN the `useradd` command succeeds THE SYSTEM SHALL return a
   `UbuntuUserGroupActionResult` with `status=SUCCESS`, `kind="user"`, and the captured
   `exit_code`, `stdout`, and `stderr`.
5. WHEN the `useradd` command fails THE SYSTEM SHALL return a
   `UbuntuUserGroupActionResult` with `status=FAILED`, `kind="user"`, and the captured
   `exit_code`, `stdout`, and `stderr`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`, `kind="user"`, and the `argv`
   that would have been dispatched in `command`.

---

### Requirement 2: Delete a Local User

**User Story:** As a maintenance coordinator, I want to delete a local user, optionally
removing their home directory, so that dismissed accounts leave no orphaned resources.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_user(name, remove_home=True)` THE SYSTEM SHALL
   dispatch `userdel -r <name>` through the command runner with `elevated=True` when
   configured.
2. WHEN the coordinator calls `delete_user(name, remove_home=False)` THE SYSTEM SHALL
   dispatch `userdel <name>` through the command runner.
3. WHEN the target user does not exist (detected via `getent passwd <name>`) THE SYSTEM
   SHALL skip execution and return a `UbuntuUserGroupActionResult` with `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 3: Modify a Local User

**User Story:** As a maintenance coordinator, I want to modify a subset of an existing
user's properties, so that shell, home, primary group, comment, or UID can be updated
without recreating the account.

#### Acceptance Criteria

1. WHEN the coordinator calls `modify_user(name, changes)` THE SYSTEM SHALL construct a
   `usermod` invocation with only the flags corresponding to non-`None` fields of
   `UbuntuUserModSpec` (`-s`, `-d` with `-m` when `move_home=True`, `-g`, `-c`, `-u`) and
   dispatch it with `elevated=True` when configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `UbuntuUserNotFoundError` before any command is dispatched.
3. WHEN all non-`None` fields already match the current `UbuntuUserInfo` THE SYSTEM SHALL
   skip execution and return a `UbuntuUserGroupActionResult` with `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 4: Query a Single User

**User Story:** As a maintenance coordinator, I want to query a single local user's
record, so that downstream logic can branch on existence and properties without having
to parse `/etc/passwd` itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_user(name)` THE SYSTEM SHALL dispatch
   `getent passwd <name>` through the command runner without elevation.
2. WHEN `getent` exits `0` THE SYSTEM SHALL parse the colon-separated line into a
   `UbuntuUserInfo` with `name`, `uid`, `gid`, `comment`, `home`, and `shell`, then
   dispatch `passwd -S <name>` through the runner without elevation to determine the
   `locked` flag.
3. WHEN `getent` exits `2` (user not found) THE SYSTEM SHALL return `None`.
4. WHEN `getent` exits with any other non-zero code THE SYSTEM SHALL raise a
   `UbuntuUserGroupQueryError` carrying the exit code and stderr.

---

### Requirement 5: List All Local Users

**User Story:** As a maintenance coordinator, I want to list every local user in the
standard local UID range, so that reporting and audit workflows enumerate the set of
maintenance targets.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_users()` THE SYSTEM SHALL dispatch `getent passwd`
   through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line into a `UbuntuUserInfo`
   and return only entries whose UID is either below `UID_MIN` (system users) or within
   the standard local UID range read from `/etc/login.defs` via the command runner.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuUserGroupQueryError` with the
   exit code and stderr.
4. WHEN the coordinator wants lock-state information THE SYSTEM SHALL defer that query
   to `get_user(name)` — `list_users()` SHALL set `locked=False` on every returned
   entry to keep the listing fast.

---

### Requirement 6: Set a User's Password

**User Story:** As a maintenance coordinator, I want to set a user's password without
leaking it into command arguments or audit logs, so that credentials remain confidential.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_password(name, password)` THE SYSTEM SHALL dispatch
   `chpasswd` through the command runner with `elevated=True` when configured and pass
   the string `"<name>:<password>\n"` via stdin.
2. WHEN the `UbuntuUserGroupActionResult` is constructed THE SYSTEM SHALL set `command`
   to `["chpasswd"]` (or `["sudo", "chpasswd"]` when the runner reports elevation) —
   the plaintext password SHALL NOT appear in `command`, `stdout`, or `stderr`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `UbuntuUserNotFoundError` before any command is dispatched.
4. WHEN `chpasswd` fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Lock a User Account

**User Story:** As a maintenance coordinator, I want to lock a user account so that the
user cannot log in without deleting the account.

#### Acceptance Criteria

1. WHEN the coordinator calls `lock_account(name)` THE SYSTEM SHALL dispatch
   `passwd -l <name>` through the command runner with `elevated=True` when configured.
2. WHEN the current `UbuntuUserInfo.locked` is already `True` THE SYSTEM SHALL skip
   execution and return a `UbuntuUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `UbuntuUserNotFoundError`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult`
   with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Unlock a User Account

**User Story:** As a maintenance coordinator, I want to unlock a previously locked
account so that the user can resume interactive use.

#### Acceptance Criteria

1. WHEN the coordinator calls `unlock_account(name)` THE SYSTEM SHALL dispatch
   `passwd -u <name>` through the command runner with `elevated=True` when configured.
2. WHEN the current `UbuntuUserInfo.locked` is already `False` THE SYSTEM SHALL skip
   execution and return a `UbuntuUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `UbuntuUserNotFoundError`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult`
   with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 9: Create a Local Group

**User Story:** As a maintenance coordinator, I want to create a local group from a
supplied `UbuntuGroupSpec`, so that downstream user memberships can target it.

#### Acceptance Criteria

1. WHEN the coordinator provides a `UbuntuGroupSpec` THE SYSTEM SHALL construct a
   `groupadd` invocation with `-g <gid>` when the GID is set, `-r` when
   `system_group=True`, and the group name as the final positional argument, dispatching
   the argv with `elevated=True` when configured.
2. WHEN a group with the same name already exists (detected via `getent group <name>`)
   THE SYSTEM SHALL skip execution and return a `UbuntuUserGroupActionResult` with
   `status=SKIPPED` and `kind="group"`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Delete a Local Group

**User Story:** As a maintenance coordinator, I want to delete a local group so that
stale group entries do not accumulate after their last referring user is removed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_group(name)` THE SYSTEM SHALL dispatch
   `groupdel <name>` through the command runner with `elevated=True` when configured.
2. WHEN the target group does not exist THE SYSTEM SHALL skip execution and return a
   `UbuntuUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 11: Query a Single Group

**User Story:** As a maintenance coordinator, I want to query a single group's record,
so that membership checks can branch on current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_group(name)` THE SYSTEM SHALL dispatch
   `getent group <name>` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the colon-separated line into a
   `UbuntuGroupInfo` with `name`, `gid`, and `members` (the fourth colon-separated
   field, split on `,`).
3. WHEN the group does not exist (`getent` exits `2`) THE SYSTEM SHALL return `None`.
4. WHEN `getent` exits with any other non-zero code THE SYSTEM SHALL raise a
   `UbuntuUserGroupQueryError`.

---

### Requirement 12: List All Local Groups

**User Story:** As a maintenance coordinator, I want to list every local group, so that
reporting workflows can enumerate groups for cross-reference.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_groups()` THE SYSTEM SHALL dispatch
   `getent group` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse every line into a `UbuntuGroupInfo`
   and return the resulting list in the order `getent` returned it.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuUserGroupQueryError` with the
   exit code and stderr.

---

### Requirement 13: Add a User to a Group

**User Story:** As a maintenance coordinator, I want to add a user to a supplementary
group, so that the user inherits the permissions associated with the group.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_to_group(user, group)` THE SYSTEM SHALL dispatch
   `gpasswd -a <user> <group>` through the command runner with `elevated=True` when
   configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `UbuntuUserNotFoundError` before any command is dispatched.
3. WHEN the target group does not exist THE SYSTEM SHALL raise
   `UbuntuGroupNotFoundError` before any command is dispatched.
4. WHEN the user is already a member of the group THE SYSTEM SHALL skip execution and
   return a `UbuntuUserGroupActionResult` with `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult`
   with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 14: Remove a User from a Group

**User Story:** As a maintenance coordinator, I want to remove a user from a
supplementary group, so that the user loses the permissions associated with the group.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_from_group(user, group)` THE SYSTEM SHALL dispatch
   `gpasswd -d <user> <group>` through the command runner with `elevated=True` when
   configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `UbuntuUserNotFoundError` before any command is dispatched.
3. WHEN the target group does not exist THE SYSTEM SHALL raise
   `UbuntuGroupNotFoundError` before any command is dispatched.
4. WHEN the user is not a member of the group THE SYSTEM SHALL skip execution and
   return a `UbuntuUserGroupActionResult` with `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuUserGroupActionResult`
   with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `UbuntuUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 15: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all user and group write commands
to run with the privileges required by `shadow-utils`, so that write operations succeed
on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (create_user,
   delete_user, modify_user, set_password, lock_account, unlock_account, create_group,
   delete_group, add_to_group, remove_from_group).
2. WHEN performing read-only operations (get_user, list_users, get_group, list_groups)
   THE SYSTEM SHALL NOT pass `elevated=True`, regardless of configuration.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the command runner to surface the resulting permission error as a
   `UbuntuUserGroupActionResult` with `status=FAILED` for writes or as a
   `UbuntuUserGroupQueryError` for reads.
