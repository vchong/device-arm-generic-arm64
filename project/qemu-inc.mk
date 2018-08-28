# Copyright (C) 2018 The Android Open Source Project
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

PROJECT_QEMU_INC_LOCAL_DIR := $(GET_LOCAL_DIR)

include project/$(QEMU_TRUSTY_PROJECT).mk

MODULES += \
	trusty/user/app/storage/rpmb_dev \

ATF_DEBUG := 1
ATF_PLAT := qemu
ATF_WITH_TRUSTY_GENERIC_SERVICES := true
ATF_BUILD_BASE := $(abspath $(BUILDDIR)/atf)
ATF_TOOLCHAIN_PREFIX := $(ARCH_arm64_TOOLCHAIN_PREFIX)
ATF_ROOT := external/arm-trusted-firmware
include project/qemu-atf-inc.mk

QEMU_ROOT := external/qemu
QEMU_BUILD_BASE := $(abspath $(BUILDDIR)/qemu-build)
QEMU_ARCH := aarch64
include project/qemu-qemu-inc.mk

EXTRA_BUILDRULES += external/trusty/test-runner/test-runner-inc.mk
TEST_RUNNER_BIN := $(BUILDDIR)/test-runner/test-runner.bin

RUN_QEMU_SCRIPT := $(BUILDDIR)/run-qemu

ATF_OUT_COPIED_FILES := \
	$(ATF_OUT_DIR)/firmware.android.dts \
	$(ATF_OUT_DIR)/run-qemu-helper \

$(ATF_OUT_COPIED_FILES): $(ATF_OUT_DIR)/% : $(PROJECT_QEMU_INC_LOCAL_DIR)/qemu/%
	@echo copying $@
	@mkdir -p $(ATF_OUT_DIR)
	@cp $< $@

$(RUN_QEMU_SCRIPT): QEMU_BIN := $(QEMU_BIN)
$(RUN_QEMU_SCRIPT): ATF_OUT_DIR := $(ATF_OUT_DIR)
$(RUN_QEMU_SCRIPT): $(ATF_OUT_COPIED_FILES) $(TEST_RUNNER_BIN) .PHONY
	ln -sf "$(abspath $(BUILDDIR)/lk.bin)" "$(ATF_OUT_DIR)/bl32.bin"
	ln -sf "$(abspath $(BUILDDIR)/test-runner/test-runner.bin)" "$(ATF_OUT_DIR)/bl33.bin"
	ln -sf "$(abspath $(BUILDDIR)/host_tools/rpmb_dev)" "$(ATF_OUT_DIR)/rpmb_dev"

	@echo generating $@
	@echo "#!/bin/sh" >$@
	@echo 'cd "$(ATF_OUT_DIR)"' >>$@
	@echo 'QEMU="$(QEMU_BIN)" ./run-qemu-helper "$$@"' >>$@
	@chmod +x $@

EXTRA_BUILDDEPS += $(RUN_QEMU_SCRIPT)
