#
# Copyright (c) 2019, Google, Inc. All rights reserved
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

# Inputs:
# LINUX_ARCH contains the architecture to build for (Global)
# Outputs:
# LINUX_BUILD_DIR contains the path to the built linux kernel sources

# This Makefile will build the Linux kernel with our configuration.

LINUX_BUILD_DIR := $(abspath $(BUILDDIR)/linux-build)
ifndef LINUX_ARCH
	$(error LINUX_ARCH must be specified)
endif
LINUX_IMAGE := $(LINUX_BUILD_DIR)/arch/$(LINUX_ARCH)/boot/Image

$(LINUX_IMAGE): LINUX_SRC := external/linux
$(LINUX_IMAGE): LINUX_DEFCONFIG := trusty_qemu_defconfig
$(LINUX_IMAGE): LINUX_BUILD_DIR := $(LINUX_BUILD_DIR)
$(LINUX_IMAGE): LINUX_MAKE_ENV += ARCH=$(LINUX_ARCH)
$(LINUX_IMAGE): LINUX_MAKE_ENV += CROSS_COMPILE=$(ARCH_$(LINUX_ARCH)_TOOLCHAIN_PREFIX)
$(LINUX_IMAGE): LINUX_MAKE_ARGS += -C $(LINUX_SRC)
$(LINUX_IMAGE): LINUX_MAKE_ARGS += O=$(LINUX_BUILD_DIR)
$(LINUX_IMAGE): .PHONY
	$(LINUX_MAKE_ENV) $(MAKE) $(LINUX_MAKE_ARGS) $(LINUX_DEFCONFIG)
	$(LINUX_MAKE_ENV) $(MAKE) $(LINUX_MAKE_ARGS)

# Add LINUX_IMAGE to the list of project dependencies
EXTRA_BUILDDEPS += $(LINUX_IMAGE)

LINUX_IMAGE :=
