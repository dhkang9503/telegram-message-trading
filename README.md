# Telegram Message Trading — Parser Model Pipeline

특정 BTCUSDT 텔레그램 리딩 채널의 한국어 메시지를 거래 액션 JSON으로 변환하는
Qwen3-0.6B LoRA 모델의 데이터, 학습, 평가, GGUF export, 배포 파이프라인입니다.

## 현재 목표

첫 번째 목표는 **초기 모델에서 사용한 동일 데이터와 동일 학습 설정으로 Q8_0 모델을
재생성하고, 현재 배포 모델의 성능을 재현할 수 있는지 확인하는 것**입니다.

- 초기 데이터: train 1,604 / validation 324 / test 325
- assistant 정답 토큰에만 loss 적용
- LoRA: r=16, alpha=32, dropout=0.05
- 5 epochs, learning rate 1e-4
- 기본 export: Q8_0
- 배포 차단 기준: `OPEN_TO_CLOSE > 0` 등 치명적 방향 전환 오류

기존 Colab 노트북은 `notebooks/legacy_colab_pipeline.ipynb`에 보존했습니다.

## 저장소 구조

```text
.
├── .github/workflows/
│   ├── train.yml
│   └── deploy.yml
├── configs/train.yaml
├── data/
│   ├── base/
│   │   ├── train.jsonl
│   │   ├── validation.jsonl
│   │   ├── test.jsonl
│   │   └── manifest.json
│   ├── feedback/
│   │   ├── mistakes_pending.jsonl
│   │   └── mistakes_labeled.jsonl
│   └── regression/live_failures.jsonl
├── modal/train_app.py
├── scripts/
│   ├── build_train_dataset.py
│   ├── compare_baseline.py
│   ├── deploy_ec2.sh
│   ├── evaluate.py
│   └── validate_dataset.py
└── notebooks/legacy_colab_pipeline.ipynb
```

## 데이터 역할

### `data/base`

초기 모델의 원본 train/validation/test입니다. 비교 기준을 보존하기 위해 test는
수정하지 않습니다.

### `mistakes_pending.jsonl`

운영 서버의 mistake bot이 수집한 **방장 메시지 + 잘못된 모델 출력**입니다.
정답이 없으므로 학습에 사용하지 않습니다.

### `mistakes_labeled.jsonl`

검수 후 정답 액션이 확정된 feedback입니다. 학습 시 다음처럼 결합됩니다.

```text
data/base/train.jsonl + data/feedback/mistakes_labeled.jsonl
```

지원되는 feedback 형식은 두 가지입니다.

1. 기존 ChatML `messages` 형식
2. mistake bot 형식에 아래 label을 채운 형식

```json
{
  "source": {"message": "물타기 취소하고 걍 탈게요!"},
  "label": {
    "status": "approved",
    "correct_actions": [
      {"type": "CANCEL_ADD", "price": null},
      {"type": "ADD", "price": null}
    ]
  }
}
```

`model.actions`의 잘못된 예측은 절대 학습 정답으로 사용하지 않습니다.

### `live_failures.jsonl`

실제 운영 실패 사례의 재발 여부를 확인하는 별도 regression set입니다. 같은 문장을
train에도 넣었다면 이 점수는 일반화 성능이 아니라 재발 방지 점검으로 해석합니다.

## 로컬 데이터 검사

```bash
python scripts/validate_dataset.py --root .
python -m pip install pytest
pytest -q
```

현재 원본 데이터는 같은 표현의 반복 예제가 많고 split 사이 일부 표현이 겹칩니다.
초기 모델 재현이 우선이므로 첫 버전에서는 데이터를 변경하지 않고 검사 보고서에만
기록합니다.

## Modal 준비

GitHub Actions에서 Modal을 호출하려면 repository secrets에 다음 두 값을 넣습니다.

```text
MODAL_TOKEN_ID
MODAL_TOKEN_SECRET
```

Modal 공식 CI 방식과 동일하게 환경변수로 인증합니다. Volume은 코드에서
`create_if_missing=True`로 최초 실행 시 생성됩니다.

- `telegram-parser-artifacts`: run별 모델, 평가 결과, manifest
- `telegram-parser-hf-cache`: Hugging Face 모델 cache

여러 Modal Environment를 사용한다면 repository variable도 추가합니다.

```text
MODAL_ENVIRONMENT=main
```

## 학습 실행

GitHub의 **Actions → Train parser model → Run workflow**에서 실행합니다.

기본값:

```text
quant_types = Q8_0
```

필요할 때 다음처럼 세 종류를 모두 만들 수 있습니다.

```text
Q4_K_M,Q5_K_M,Q8_0
```

학습 workflow는 다음 순서로 동작합니다.

```text
dataset validation
→ Modal A100 LoRA training
→ validation loss
→ adapter merge
→ F16 GGUF
→ requested quantization
→ llama-server 기반 deterministic test
→ release gate
→ Modal Volume 저장
```

run 결과는 다음 위치에 보존됩니다.

```text
telegram-parser-artifacts/runs/<run_id>/
```

`manifest.json`에는 git SHA, 데이터 hash, 학습 metric, GGUF hash, test 결과,
배포 가능 여부가 들어갑니다.

학습이 완료됐다고 자동 배포하지 않습니다.

## 배포 workflow

`Deploy parser model`은 별도 `workflow_dispatch`입니다. `run_id`를 직접 입력해야
동작하며 `production` GitHub Environment를 사용합니다.

필요한 GitHub secrets:

```text
EC2_HOST
EC2_USER
EC2_SSH_KEY
```

필요한 repository variables:

```text
EC2_MODEL_ROOT=/home/ubuntu/models
EC2_CURRENT_MODEL_LINK=/home/ubuntu/models/current.gguf
EC2_LLAMA_SERVICE=llama-server
EC2_LLAMA_HEALTH_URL=http://127.0.0.1:8080/health
```

실제 서버 경로와 systemd 서비스명에 맞게 설정하십시오. workflow는 Q8 파일 hash를
검증하고, 버전 디렉터리에 설치한 뒤 symlink를 교체하고 health check 실패 시 이전
모델로 rollback합니다.

## Release gate

기준은 `configs/train.yaml`에서 관리합니다.

기본 정책:

- JSON valid 100%
- exact match 93.5% 이상
- LONG↔SHORT 오류 0
- OPEN↔CLOSE 오류 0

gate 실패 run도 학습 결과와 평가 파일은 남지만 기본 deploy workflow가 차단합니다.
`force_ineligible`은 결과를 직접 검토한 경우에만 사용합니다.

## 재현성 메모

업로드된 노트북에서 확인한 주요 환경과 설정을 고정했습니다.

- Python 3.12
- PyTorch 2.11.0 + CUDA 12.8
- Transformers 4.51.3
- PEFT 0.15.2
- llama.cpp commit `91d2fc387529940230555abd297a8b5e99737d3f`

GPU 종류가 T4에서 A100으로 바뀌므로 부동소수점 연산과 실행 환경 차이 때문에 결과가
완전히 동일하다고 보장할 수는 없습니다. 따라서 exact metric뿐 아니라 실제 오답 집합과
치명적 오류를 함께 비교합니다.
