#
# Copyright (c) 2018, Google, Inc. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

#
#  This makefile containts rules for building QEMU for running Trusty.
#  It is expected that it will be included by the project that uses QEMU
#  and the caller will configure the following variables:
#
#      QEMU_ROOT       - Root of qemu project
#      QEMU_BUILD_BASE - location that will be used to store temp files and
#                        build results.
#      QEMU_ARCH       - qemu arch to build
#
#  The following variable is returned to the caller:
#      QEMU_BIN        - resulting qemu image
#
#

QEMU_BIN:=$(QEMU_BUILD_BASE)/$(QEMU_ARCH)-softmmu/qemu-system-$(QEMU_ARCH)
QEMU_MAKEFILE:=$(QEMU_BUILD_BASE)/Makefile

$(QEMU_MAKEFILE): QEMU_ROOT:=$(QEMU_ROOT)
$(QEMU_MAKEFILE): QEMU_BUILD_BASE:=$(QEMU_BUILD_BASE)
$(QEMU_MAKEFILE):
	mkdir -p $(QEMU_BUILD_BASE)
	#--with-git=true sets the "git" program to /bin/true - it essentially disables git
	#--disable-git-update may look like what we want, but it requests manual intervention, not disables git
	# TODO(b/148904400): Our prebuilt Clang can't build QEMU yet, and there is no
	# prebuilts GCC, i.e. currently we can only build QEMU with host toolchain. On
	# some hosts compiler will complain about stringop truncation.
	cd $(QEMU_BUILD_BASE) && $(abspath $(QEMU_ROOT)/configure) \
		--target-list=aarch64-softmmu,arm-softmmu --with-git=true --disable-werror

$(QEMU_BIN): QEMU_BUILD_BASE:=$(QEMU_BUILD_BASE)
$(QEMU_BIN): $(QEMU_MAKEFILE) .PHONY
	$(MAKE) -C $(QEMU_BUILD_BASE)

# Add QEMU_BIN to the list of project dependencies
EXTRA_BUILDDEPS += $(QEMU_BIN)

QEMU_ROOT:=
