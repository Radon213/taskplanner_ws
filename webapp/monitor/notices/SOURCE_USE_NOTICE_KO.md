# 소스·데이터 사용 고지

이 프로젝트는 Taskplanner 공개 ROS Bridge와 SurgiMate UI의 연동 및 표시 검증을 위한 예제입니다.

- 의료 판단, 임상 성능 평가, 로봇 명령 승인 또는 물리 로봇 실행의 근거가 아닙니다.
- 다음 도구 예측은 advisory이며 제어 명령이 아닙니다.
- 실제 영상과 `speech.text`는 민감정보로 취급합니다.
- 실제 payload를 localStorage, analytics, 오류 로그, fixture, 화면 녹화 또는 공개 저장소에 저장하지 마십시오.
- JSON 합성 fixture와 [`/monitor/dummy-data.json`](/monitor/dummy-data.json)에는 `synthetic: true`가 표시되며 실제 수술 기록이 아닙니다. 함께 제공되는 합성 CSV도 같은 검증 범위의 자료입니다.
- Dummy mode의 외부 편집 파일에는 UI 확인용 합성 값만 사용하고 실제 gateway/run ID, 환자·의료진 정보, 영상 URL 또는 음성 자유문장을 넣지 마십시오.
- Settings에서 선택한 로컬 Dummy JSON은 브라우저가 `File.text()`로 읽고 현재 page session의 memory에서만 검증·재생합니다. 앱은 파일을 upload하지 않으며 `File` 객체, 표시용 파일명과 payload를 `localStorage`에 저장하지 않습니다. reload 후에는 저장된 URL fixture를 사용하므로 필요하면 파일을 다시 선택해야 합니다.
- 로컬 JSON도 `taskplanner.ui_contract_fixture.v1`, `synthetic: true`, 최대 512KB 제한과 공개 토픽 allowlist를 따릅니다. envelope 검증 통과가 임상 안전성이나 데이터 비식별화를 보증하지 않으므로 실제 자료를 선택하지 마십시오.
- 실제 MCAP, 수술 영상, 음성 자유문장을 이 저장소에 포함하지 마십시오.
- Figma에서 전달된 수술 이미지, 인물 이미지, 도구 asset의 외부 재배포 권한은 별도로 확인해야 합니다.

Taskplanner 서버의 공개 계약과 allowlist가 최종 보안 경계입니다. UI 코드만으로 publish/service/action 차단을 대체할 수 없습니다.
