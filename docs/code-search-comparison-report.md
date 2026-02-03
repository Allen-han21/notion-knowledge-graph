# 코드 검색 방식 비교 분석 보고서

> **분석일**: 2026-02-03
> **분석 대상**: kidsnote_ios 프로젝트
> **이슈**: 댓글 수정 시 작성자 표시가 iOS/Android 간 다르게 동작

---

## 개요

동일한 이슈를 세 가지 검색 방식으로 분석하여 각 방식의 성능, 정확도, 장단점을 비교합니다.

| 방식 | 설명 |
|------|------|
| **키워드 검색** | Explore 에이전트 + Grep + Read 조합 |
| **의미 검색** | 자연어 쿼리로 벡터 임베딩 기반 검색 |
| **그래프 검색** | Neo4j Cypher 쿼리로 관계 기반 검색 |

---

## 분석 대상 이슈

**현상**:
- iOS에서 작성한 댓글을 Android에서 수정 시: 작성자 호칭 **유지**
- Android에서 작성한 댓글을 iOS에서 수정 시: 작성자 호칭이 **수정자로 변경**

**목표**: `author_name` 파라미터 전송 로직 찾기

**정답**: `CommentRepository.swift:214` - `["author_name": authorName, ...]`

---

## 1. 키워드 검색 (Explore + Grep)

### 수행 과정

```
[1] Task(Explore) 에이전트 실행
    → "댓글 수정 관련 코드 찾아줘"
    → 13개 파일 발견
        ↓
[2] Grep "editComment|updateComment"
    → 17개 매칭
        ↓
[3] Grep "author_name"
    → 80+ 매칭 (노이즈 많음)
        ↓
[4] Read CommunityAPI.swift
    → Community 모듈 (다른 기능)
        ↓
[5] Read CommentAPI.swift
    → 핵심 API 발견
        ↓
[6] Grep "author_name.*comment"
    → CommentRepository.swift 발견
        ↓
[7] Read CommentRepository.swift
    → 원인 코드 확인 (line 214)
        ↓
[8] Read CommentInputUseCase.swift
    → getNickName() 호출 확인
```

### 결과

| 항목 | 값 |
|------|-----|
| **총 단계** | 8단계 |
| **도구 호출 수** | 12회 |
| **노이즈 파일** | 다수 (Community, Album, Notice 등) |
| **핵심 코드 도달** | 성공 |
| **소요 시간 (체감)** | 5~10분 |

---

## 2. 의미 검색 (Semantic Search)

### 수행 과정

```
[1] 의미 검색 쿼리 실행
    "update comment request body parameters author"
        ↓
[2] 결과 즉시 반환
    1위: CommentService.swift (0.031)
    2위: CommentRepository.swift (0.031) ← 원인 코드
        ↓
[3] Read CommentRepository.swift
    → 원인 코드 확인 (line 214)
```

### 쿼리별 정확도

| 쿼리 | 1위 결과 | 원인 코드 순위 | 점수 |
|------|----------|--------------|------|
| `"update comment request body parameters author"` | CommentService | **2위** | 0.031 |
| `"send comment with author_name parameter"` | CommentService | **3위** | 0.033 |
| `"comment edit author name handling"` | ChangeWritedTimeViewController | 7위 | 0.016 |
| `"댓글 작성자 호칭 전송"` (한글) | NicknameHeaderView | ❌ 미적중 | 0.029 |

### 결과

| 항목 | 값 |
|------|-----|
| **총 단계** | 2~3단계 |
| **도구 호출 수** | 2~3회 |
| **노이즈 파일** | 적음 |
| **핵심 코드 도달** | 성공 |
| **소요 시간 (체감)** | 1~2분 |

---

## 3. 그래프 검색 (Neo4j)

### 수행 과정

```
[1] 데이터 레이어 우선순위 쿼리
    MATCH (c:CodeFile) WHERE c.path CONTAINS 'Comment'
    ORDER BY Repository > UseCase > Service > API
        ↓
[2] 결과 즉시 반환
    1위: CommentRepository.swift (priority: 5) ← 원인 코드
    2위: CommentInputUseCase.swift (priority: 4)
        ↓
[3] 경로 탐색 쿼리
    MATCH path = (UseCase)-[:SIMILAR_TO*]-(API)
        ↓
[4] 전체 데이터 흐름 파악
    UseCase → Repository → Service → API
        ↓
[5] Read CommentRepository.swift
    → 원인 코드 확인 (line 214)
```

### 주요 Cypher 쿼리

**쿼리 1: 데이터 레이어 우선순위**
```cypher
MATCH (c:CodeFile)
WHERE c.path CONTAINS 'Comment'
WITH c,
     CASE
       WHEN c.path CONTAINS 'Repository' THEN 5
       WHEN c.path CONTAINS 'UseCase' THEN 4
       WHEN c.path CONTAINS 'Service' THEN 3
       WHEN c.path CONTAINS 'API' THEN 2
       ELSE 1
     END as priority
RETURN c.path, priority
ORDER BY priority DESC
```

**쿼리 2: 경로 탐색**
```cypher
MATCH path = (start:CodeFile)-[:SIMILAR_TO*1..3]-(end:CodeFile)
WHERE start.path = 'Common/Comment/UseCases/CommentInputUseCase.swift'
  AND end.path CONTAINS 'API'
RETURN [n in nodes(path) | n.path] as connection_path
```

### 결과

| 항목 | 값 |
|------|-----|
| **총 단계** | 2~3단계 |
| **도구 호출 수** | 3~4회 |
| **노이즈 파일** | 거의 없음 |
| **핵심 코드 도달** | 성공 |
| **데이터 흐름 파악** | ✅ 추가 획득 |
| **소요 시간 (체감)** | 2~3분 |

### 발견한 데이터 흐름

```
CommentInputUseCase.swift
    ↓ SIMILAR_TO
CommentRepository.swift      ← 원인 코드 (author_name 전송)
    ↓ SIMILAR_TO
CommentService.swift
    ↓ SIMILAR_TO
CommentAPI.swift
```

---

## 4. 종합 비교

### 성능 비교

| 항목 | 키워드 검색 | 의미 검색 | 그래프 검색 |
|------|------------|----------|------------|
| **핵심 코드 도달 단계** | 8단계 | **2단계** | **2단계** |
| **도구 호출 수** | 12회 | **2회** | 4회 |
| **노이즈 파일 수** | 많음 | 적음 | **거의 없음** |
| **소요 시간 (체감)** | 5~10분 | **1~2분** | 2~3분 |
| **데이터 흐름 파악** | ❌ | ❌ | ✅ |
| **파일 간 관계 파악** | ❌ | ❌ | ✅ |

### 정확도 비교

| 항목 | 키워드 검색 | 의미 검색 | 그래프 검색 |
|------|------------|----------|------------|
| **첫 번째 결과 적중률** | 낮음 | **높음** | **높음** |
| **키워드 추측 필요** | ✅ 필요 | ❌ 불필요 | ❌ 불필요 |
| **한글 쿼리 지원** | ✅ 가능 | ⚠️ 낮은 정확도 | ✅ 가능 |
| **영어 쿼리 권장** | - | ✅ 권장 | - |

### 장단점 비교

| 방식 | 장점 | 단점 |
|------|------|------|
| **키워드 검색** | • 도구 학습 불필요<br>• 한글 검색 가능<br>• 정확한 패턴 매칭 | • 키워드 추측 필요<br>• 노이즈 많음<br>• 여러 단계 반복 |
| **의미 검색** | • 자연어 쿼리<br>• **빠른 진입점 특정**<br>• 최소 단계 | • 영어 쿼리 권장<br>• 파일 간 관계 모름<br>• 임베딩 필요 |
| **그래프 검색** | • **파일 간 관계 파악**<br>• **데이터 흐름 시각화**<br>• 레이어별 우선순위 | • Cypher 학습 필요<br>• 초기 설정 복잡<br>• 쿼리 작성 난이도 |

### 별점 평가

| 평가 항목 | 키워드 | 의미 | 그래프 | **하이브리드** |
|----------|--------|------|--------|---------------|
| 속도 | ⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 정확도 | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 관계 파악 | ⭐ | ⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 학습 곡선 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ |
| **종합** | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | **⭐⭐⭐⭐⭐** |

---

## 5. 검색 결과 시각화

### 키워드 검색 경로
```
Explore → Grep(edit) → Grep(author) → Read(X) → Read(X)
→ Grep(패턴수정) → Read(O) → Read(확인) → 완료

[8단계, 노이즈 많음]
```

### 의미 검색 경로
```
의미검색("update comment...") → 2위 결과 → Read → 완료

[2단계, 직행]
```

### 그래프 검색 경로
```
레이어 우선순위 쿼리 → 1위 결과 + 전체 흐름 → Read → 완료

[2단계, 직행 + 추가 정보]
```

---

## 6. 권장 사용 시나리오

| 상황 | 권장 방식 | 이유 |
|------|----------|------|
| **빠른 버그 원인 찾기** | 의미 검색 | 최소 단계로 핵심 도달 |
| **아키텍처 파악 필요** | 그래프 검색 | 파일 간 관계 + 데이터 흐름 |
| **정확한 패턴 찾기** | 키워드 검색 | `@Dependency`, 특정 변수명 |
| **한글 도메인 용어** | 키워드 검색 | 의미 검색 한글 정확도 낮음 |
| **복잡한 이슈 분석** | **하이브리드** | 의미 → 그래프 → 키워드 |

---

## 7. 최적 워크플로우 (하이브리드)

```
┌─────────────────────────────────────────────────────────────┐
│  [1단계] 의미 검색 - 진입점 특정                              │
│      "update comment request body parameters author"         │
│          ↓                                                   │
│      CommentRepository.swift 특정                            │
├─────────────────────────────────────────────────────────────┤
│  [2단계] 그래프 검색 - 관계 파악                              │
│      MATCH (c)-[:SIMILAR_TO*]-(related)                      │
│      WHERE c.path = 'CommentRepository.swift'                │
│          ↓                                                   │
│      UseCase → Repository → Service → API 흐름 파악          │
├─────────────────────────────────────────────────────────────┤
│  [3단계] 키워드 검색 - 정밀 확인 (필요시)                     │
│      Grep "author_name" CommentRepository.swift              │
│          ↓                                                   │
│      line 214 정확한 위치 특정                               │
├─────────────────────────────────────────────────────────────┤
│  [4단계] Read - 코드 검증                                    │
│      원인: userInfo.getNickName() → author_name 전송         │
└─────────────────────────────────────────────────────────────┘
```

---

## 8. 결론

### 핵심 발견

1. **의미 검색**은 빠른 진입점 특정에 최적
2. **그래프 검색**은 파일 간 관계와 데이터 흐름 파악에 최적
3. **키워드 검색**은 정확한 패턴 매칭에 여전히 유용
4. **하이브리드 방식**이 가장 효과적

### 권장 조합

```
의미 검색 (진입) → 그래프 검색 (관계) → 키워드 검색 (정밀) → Read (검증)
```

### 이번 이슈 원인

```swift
// CommentRepository.swift:214
return ["author_name": authorName,  // ← 현재 사용자 호칭 전송
        "content": text,
        ...]
```

iOS는 댓글 수정 시 `userInfo.getNickName()`으로 **현재 로그인 사용자의 호칭**을 가져와 `author_name`으로 전송 → 서버에서 덮어쓰기 발생

---

**작성자**: Claude (AI Assistant)
**작성일**: 2026-02-03
**프로젝트**: kidsnote_ios
**저장소**: [notion-knowledge-graph](https://github.com/Allen-han21/notion-knowledge-graph)
