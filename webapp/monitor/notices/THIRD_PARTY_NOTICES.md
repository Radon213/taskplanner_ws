# Third-Party Notices

이 프로젝트의 직접 JavaScript 의존성은 [webapp package-lock.json](../../package-lock.json)에 고정되어 있습니다.

| 구성요소 | 버전 | 용도 |
| --- | --- | --- |
| `roslib-monitor` (`roslib` alias) | `2.1.0` | 격리된 모니터 ROS Bridge WebSocket 구독 |
| `vite` | `8.2.1` | Taskplanner 개발 서버와 프로덕션 번들 |
| `Pretendard Variable` | `1.3.9` | Figma UI 지정 한글·UI 웹폰트 |
| `JetBrains Mono Variable` | `5.3.0` Fontsource 패키지 | Figma 데이터·도구명 monospace 웹폰트 |
| `Inter Variable` | `5.3.0` Fontsource 패키지 | UI font fallback |

각 패키지와 전이 의존성의 라이선스는 해당 npm 배포물의 license 파일과 metadata를 따릅니다. 배포 전 `node_modules` 및 생성 bundle에 포함되는 고지 의무를 별도로 확인하십시오.

`../assets/fonts/PretendardVariable.woff2`는 SIL Open Font License 1.1에 따라 포함되며, 원문은 `Pretendard-LICENSE.txt`에 보관합니다.

Pretendard Variable은 UI·상태·탭·badge·정보·voice·한글 영역의 로컬 기본 font입니다. JetBrains Mono와 Inter의 Latin variable WOFF2도 로컬 asset으로 포함하며, 각 SIL Open Font License 1.1 원문은 `JetBrainsMono-OFL-1.1.txt`와 `Inter-OFL-1.1.txt`에 보관합니다. 이 폰트는 외부 Google Fonts 요청 없이 로드됩니다.

`../assets/figma/`의 수술 장면, 인물, 수술 도구 및 아이콘은 프로젝트 디자인 구현을 위해 Figma에서 전달된 asset입니다. npm 오픈소스 라이선스와 별개이며 외부 배포·상업적 사용 권한은 자산 소유자에게 확인해야 합니다.
