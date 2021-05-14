# Copyright (C) 2016-2017 The Android Open Source Project
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

UBSAN_ENABLED ?= true

include project/generic-arm-inc.mk
include trusty/kernel/kerneltests-inc.mk
include trusty/user/base/usertests-inc.mk

# Only enable pattern init in test-builds, as it has runtime overhead
# and intentionally attempts to induce crashes for bad assumptions.
GLOBAL_SHARED_COMPILEFLAGS += -ftrivial-auto-var-init=pattern

# Enable hwcrypto unittest keyslots and tests
GLOBAL_DEFINES += WITH_HWCRYPTO_UNITTEST=1

TEST_BUILD := true
