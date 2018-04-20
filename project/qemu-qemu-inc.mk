#
# Copyright (c) 2018, Google, Inc. All rights reserved
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
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
	cd $(QEMU_BUILD_BASE) && $(abspath $(QEMU_ROOT)/configure) --target-list=aarch64-softmmu,arm-softmmu

$(QEMU_BIN): QEMU_BUILD_BASE:=$(QEMU_BUILD_BASE)
$(QEMU_BIN): $(QEMU_MAKEFILE) .PHONY
	$(MAKE) -C $(QEMU_BUILD_BASE)

# Add QEMU_BIN to the list of project dependencies
EXTRA_BUILDDEPS += $(QEMU_BIN)

QEMU_ROOT:=
