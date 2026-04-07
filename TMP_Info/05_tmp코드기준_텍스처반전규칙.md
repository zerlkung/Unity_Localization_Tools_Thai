# TMP 코드 기준 텍스처/글리프 Y 반전 규칙

생성시각: 2026-02-24

## 결론(핵심)

- `Unity_Font_Replacer`가 "텍스처 자체"를 뒤집어야 하는 게 아니라,
- **TMP 글리프 좌표계가 old/new 중 무엇인지에 따라 Y 좌표를 반전해서 해석해야 합니다.**

즉, 실무 규칙은 아래와 같습니다.

- old TMP 좌표(`m_glyphInfoList`): `y`를 그대로 사용 (상단 원점 기준)
- new TMP 좌표(`m_GlyphTable.m_GlyphRect`): 이미지 상단 원점(PIL)에서 읽을 때 `y_top = atlasHeight - y - h` 적용

## TMP 코드 근거

### 1) old TMP(<= 1.3.0-preview): UV 계산에 `1 - (...)` 사용

- 태그: `1.3.0`
- 파일: `Scripts/Runtime/TMP_Text.cs`
- 근거 라인(태그 기준):
  - `uv0.y = 1 - (m_cached_TextElement.y + ... + m_cached_TextElement.height) / faceInfo.AtlasHeight;`
  - `uv1.y = 1 - (m_cached_TextElement.y - ...) / faceInfo.AtlasHeight;`

해석:
- old `m_cached_TextElement.y`는 상단 원점(top-origin) 값을 쓰고,
- UV(bottom-origin)로 보낼 때 `1 -` 보정이 필요합니다.

### 2) new TMP(>= 1.4.0): UV 계산에서 `1 -` 제거, direct 사용

- 태그: `1.4.0`
- 파일: `Scripts/Runtime/TMP_Text.cs`
- 근거 라인(태그 기준):
  - `uv0.y = (m_cached_TextElement.glyph.glyphRect.y - ...) / m_currentFontAsset.atlasHeight;`
  - `uv1.y = (m_cached_TextElement.glyph.glyphRect.y + ... + glyphRect.height) / m_currentFontAsset.atlasHeight;`

해석:
- new `glyphRect.y`는 bottom-origin 좌표를 직접 사용합니다.

### 3) 공식 마이그레이션 수식(Old -> New)

- 태그: `1.4.0`
- 파일: `Scripts/Runtime/TMP_FontAsset.cs`
- 근거 라인(태그 기준):
  - `glyph.glyphRect = new GlyphRect((int)oldGlyph.x, m_AtlasHeight - (int)(oldGlyph.y + oldGlyph.height + 0.5f), ...);`

해석:
- old y(top-origin) -> new y(bottom-origin) 변환은
- **`newY = atlasHeight - (oldY + height)`**

따라서 역변환도 동일하게
- **`oldY = atlasHeight - (newY + height)`**

## 버전 경계(태그 스캔 결과)

- old UV식(`uv0.y = 1 - ...`) 마지막 태그: `1.3.0`, `1.3.0-preview`
- new UV식(직접 `glyphRect.y / atlasHeight`) 시작 태그: `1.4.0`

## Unity_Font_Replacer에 적용할 규칙

1. 소스/타겟이 `m_GlyphTable`(new)이면
- 텍스처를 상단 원점 이미지(PIL)로 읽을 때 **Y 반전 변환 필요**
- 수식: `y_top = atlasHeight - y_bottom - h`

2. 소스/타겟이 `m_glyphInfoList`(old)이면
- 기본적으로 **추가 Y 반전 없이** 해석

3. new <-> old 변환 시
- 항상 `y = atlasHeight - y - h` 수식 적용

4. 주의
- 이 규칙은 "TMP 글리프 좌표계" 기준입니다.
- PS5 swizzle/unswizzle/preview 보정은 별도 축(텍스처 저장 포맷) 문제입니다.
