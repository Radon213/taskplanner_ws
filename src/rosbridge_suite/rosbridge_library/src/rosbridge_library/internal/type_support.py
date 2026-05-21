# Software License Agreement (BSD License)
#
# Copyright (c) 2025, Fictionlab sp. z o.o.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import annotations

from typing import Any, TypeAlias, TypeVar

try:
    from rosidl_pycommon.interface_base_classes import (  # type: ignore[attr-defined]
        BaseAction,
        BaseImpl,
        BaseMessage,
        BaseService,
    )
except ImportError:
    # Jazzy does not expose these helper base classes as a public Python module.
    # rosbridge only needs broad runtime categories here for typing, so fall back to
    # permissive aliases when the helper module is unavailable.
    BaseMessage = object  # type: ignore[assignment,misc]
    BaseService = object  # type: ignore[assignment,misc]
    BaseAction = object  # type: ignore[assignment,misc]
    BaseImpl = object  # type: ignore[assignment,misc]

ROSMessage: TypeAlias = BaseMessage
ROSService: TypeAlias = BaseService
ROSAction: TypeAlias = BaseAction

# Type variables for ROS types
ROSMessageT = TypeVar("ROSMessageT", bound=ROSMessage)
ROSServiceT = TypeVar("ROSServiceT", bound=ROSService)
ROSServiceRequestT = TypeVar("ROSServiceRequestT", bound=ROSMessage)
ROSServiceResponseT = TypeVar("ROSServiceResponseT", bound=ROSMessage)
ROSActionT = TypeVar("ROSActionT", bound=ROSAction)
ROSActionGoalT = TypeVar("ROSActionGoalT", bound=ROSMessage)
ROSActionResultT = TypeVar("ROSActionResultT", bound=ROSMessage)
ROSActionFeedbackT = TypeVar("ROSActionFeedbackT", bound=ROSMessage)
ROSActionImplT = TypeVar("ROSActionImplT")
