====================================
Automation Mojo Sysman Linux Ubuntu
====================================

The ``automojo-sysman-linux-ubuntu`` package provides system-management primitives for Ubuntu (and Debian-family) Linux hosts, parallel to what ``automojo-sysman-linux-redhat`` provides for Red Hat Enterprise Linux. It implements the cross-OS contracts published by ``automojo-sysman-common`` over the standard Ubuntu admin toolchain — ``apt``/``dpkg``, ``systemctl``, ``netplan``, ``ufw``, ``journald``, AppArmor, and friends — exposed through the same ``ICommandRunner`` / ``IFileOperator`` abstractions and ``Aspects``-style behaviour parameters used elsewhere in the suite.

Status
======

40 cross-OS features implemented (50 modules total). Test suite: 814/814 passing under ``testbase``. See ``specs/3-<feature>/`` for the per-OS requirements/design/tasks for each implemented feature.

Repository Structure
====================

- ``development/`` - Runtime environment scripts.
- ``repository-setup/`` - Repository homing and setup scripts.
- ``source/packages/automojo/sysman/linux/ubuntu/`` - Implementation modules.
- ``source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/`` - testbase test suite.
- ``specs/`` - Feature specifications organised by phase and domain area.
- ``userguide/`` - Standards, conventions, and documentation.

Environment Setup
=================

When working in a Codex container:

.. code-block:: bash

   ./development/setup-agent-container

For local development:

.. code-block:: bash

   ./repository-setup/rehome-repository
   ./development/setup-environment

Running Tests
=============

Activate the virtual environment and run the testbase suite:

.. code-block:: bash

   source .venv/bin/activate
   testbase testing run --root source/testroots/testbase \
       --includes=testbase.automojo.tests.sysman.linux.ubuntu \
       --output=./.output

Standards and Conventions
=========================

- Naming conventions: ``userguide/10-02-naming-conventions.rst``
- Code organization: ``userguide/10-01-code-organization.rst``
- Coding standards: ``userguide/10-00-coding-standards.rst``

Additional Resources
====================

- User guide entry point: ``userguide/userguide.rst``
- Cross-OS contracts: ``../automojo-sysman-common/specs/contracts/``
