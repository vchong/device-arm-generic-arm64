#
# Copyright (c) 2015-2018, Google, Inc. All rights reserved
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
#  This makefile containts rules for  building ATF image for Trusty.
#  It is expected that it will be included by the project that requres ATF
#  support and the caller will configure the following variables:
#
#      ATF_ROOT       - Root of arm-trusted-firmware project
#      ATF_BUILD_BASE - location that will be used to store temp files and
#                       build results.
#      ATF_PLAT       - ATF platform to build
#      ATF_DEBUG      - ATF debug level
#      ATF_WITH_TRUSTY_GENERIC_SERVICES - Add Trusty generic services
#      ATF_TOOLCHAIN_PREFIX - AArch64 toolchain to use for building ATF
#
#  The following variable is returned to the caller:
#      ATF_OUT_DIR    - Directory containing ATF images
#
#

# set location of resulting ATF image
ifneq ($(ATF_DEBUG), 0)
ATF_OUT_DIR := $(ATF_BUILD_BASE)/$(ATF_PLAT)/debug
else
ATF_OUT_DIR:=$(ATF_BUILD_BASE)/$(ATF_PLAT)/release
endif
ATF_BIN := $(ATF_OUT_DIR)/bl31.bin

ATF_WITH_TRUSTY_GENERIC_SERVICES ?= false

ATF_MAKE_ARGS := SPD=trusty
ATF_MAKE_ARGS += CROSS_COMPILE=$(ATF_TOOLCHAIN_PREFIX)
ATF_MAKE_ARGS += PLAT=$(ATF_PLAT)
ATF_MAKE_ARGS += DEBUG=$(ATF_DEBUG)
ATF_MAKE_ARGS += BUILD_BASE=$(ATF_BUILD_BASE)

ifeq (true,$(call TOBOOL,$(ATF_WITH_TRUSTY_GENERIC_SERVICES)))
ATF_MAKE_ARGS += TRUSTY_SPD_WITH_GENERIC_SERVICES=1
endif

$(ATF_BIN): ATF_ROOT:=$(ATF_ROOT)
$(ATF_BIN): ATF_MAKE_ARGS:=$(ATF_MAKE_ARGS)
$(ATF_BIN): .PHONY
	$(MAKE) -C $(ATF_ROOT) $(ATF_MAKE_ARGS)

# Add ATF_BIN to the list of project dependencies
EXTRA_BUILDDEPS += $(ATF_BIN)

ATF_ROOT:=
ATF_BUILD_BASE:=
ATF_PLAT:=
ATF_DEBUG:=
ATF_WITH_TRUSTY_GENERIC_SERVICES:=
ATF_TOOLCHAIN_PREFIX:=
ATF_BIN:=
ATF_MAKE_ARGS:=
