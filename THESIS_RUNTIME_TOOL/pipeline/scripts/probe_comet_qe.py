from __future__ import annotations

import json
import os
import pathlib
import statistics
import time

import torch
from comet import download_model, load_from_checkpoint


MODEL_NAME = os.environ.get("COMET_MODEL", "Unbabel/wmt22-cometkiwi-da")
OUT_NAME = os.environ.get("COMET_OUT_NAME")
PROBE_SET = os.environ.get("COMET_PROBE_SET", "basic")


def case(case_id: str, src: str, mt: str, expected: str, note: str) -> dict[str, str]:
    return {"id": case_id, "src": src, "mt": mt, "expected": expected, "note": note}


def pair(
    name: str,
    src: str,
    good_mt: str,
    bad_mt: str,
    bad_expected: str,
    note: str,
) -> tuple[list[dict[str, str]], tuple[str, str]]:
    good_id = f"good_{name}"
    bad_id = f"bad_{name}"
    return (
        [
            case(good_id, src, good_mt, "good", f"good: {note}"),
            case(bad_id, src, bad_mt, bad_expected, f"bad: {note}"),
        ],
        (good_id, bad_id),
    )


def build_basic_probe() -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    specs = [
        pair(
            "regularization",
            "Regularization reduces overfitting by penalizing large weights.",
            "Điều chuẩn làm giảm quá khớp bằng cách phạt các trọng số lớn.",
            "Chuẩn hóa làm giảm quá khớp bằng cách phạt các trọng số lớn.",
            "bad_term",
            "regularization vs normalization",
        ),
        pair(
            "accuracy",
            "The validation accuracy increases after tuning the learning rate.",
            "Độ chính xác trên tập xác thực tăng sau khi điều chỉnh tốc độ học.",
            "Độ chính xác trên tập xác thực giảm sau khi điều chỉnh tốc độ học.",
            "bad_semantic",
            "increase vs decrease",
        ),
        pair(
            "dropout",
            "Dropout randomly masks hidden units during training.",
            "Dropout che ngẫu nhiên các đơn vị ẩn trong quá trình huấn luyện.",
            "Trong quá trình huấn luyện, mô hình thay đổi các tầng ẩn.",
            "bad_omission",
            "dropout/random masking omitted",
        ),
        pair(
            "batch_size",
            "The batch size controls how many examples are processed at once.",
            "Kích thước batch kiểm soát số ví dụ được xử lý cùng lúc.",
            "Kích thước batch kiểm soát số ví dụ được xử lý cùng lúc và đảm bảo mô hình luôn hội tụ.",
            "medium_bad_addition",
            "unsupported convergence guarantee",
        ),
        pair(
            "activation",
            "The activation function introduces nonlinearity into the neural network.",
            "Hàm kích hoạt đưa tính phi tuyến vào mạng nơ-ron.",
            "The activation function introduces nonlinearity into the neural network.",
            "bad_untranslated",
            "untranslated English copy",
        ),
    ]
    cases: list[dict[str, str]] = [
        case(
            "good_gradient",
            "The gradient is computed during backpropagation to update the model parameters.",
            "Gradient được tính trong quá trình lan truyền ngược để cập nhật các tham số của mô hình.",
            "good",
            "meaning and key technical terms preserved",
        ),
        case(
            "bad_wrong_subject",
            "The optimizer updates the weights, not the training data.",
            "Bộ tối ưu cập nhật dữ liệu huấn luyện, không phải các trọng số.",
            "bad_semantic",
            "object relation reversed",
        ),
    ]
    pairs: list[tuple[str, str]] = []
    for pair_cases, pair_ids in specs:
        cases.extend(pair_cases)
        pairs.append(pair_ids)
    return cases, pairs


def build_wide_probe() -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    specs = [
        pair(
            "regularization",
            "Regularization reduces overfitting by penalizing large weights.",
            "Điều chuẩn làm giảm quá khớp bằng cách phạt các trọng số lớn.",
            "Chuẩn hóa làm giảm quá khớp bằng cách phạt các trọng số lớn.",
            "bad_term",
            "regularization mistranslated as normalization",
        ),
        pair(
            "accuracy_direction",
            "The validation accuracy increases after tuning the learning rate.",
            "Độ chính xác trên tập xác thực tăng sau khi điều chỉnh tốc độ học.",
            "Độ chính xác trên tập xác thực giảm sau khi điều chỉnh tốc độ học.",
            "bad_semantic",
            "increase/decrease polarity",
        ),
        pair(
            "dropout_omission",
            "Dropout randomly masks hidden units during training.",
            "Dropout che ngẫu nhiên các đơn vị ẩn trong quá trình huấn luyện.",
            "Trong quá trình huấn luyện, mô hình thay đổi các tầng ẩn.",
            "bad_omission",
            "dropout and random masking omitted",
        ),
        pair(
            "hallucination",
            "The batch size controls how many examples are processed at once.",
            "Kích thước batch kiểm soát số ví dụ được xử lý cùng lúc.",
            "Kích thước batch kiểm soát số ví dụ được xử lý cùng lúc và đảm bảo mô hình luôn hội tụ.",
            "medium_bad_addition",
            "unsupported guarantee added",
        ),
        pair(
            "untranslated",
            "The activation function introduces nonlinearity into the neural network.",
            "Hàm kích hoạt đưa tính phi tuyến vào mạng nơ-ron.",
            "The activation function introduces nonlinearity into the neural network.",
            "bad_untranslated",
            "English copied instead of translated",
        ),
        pair(
            "subject_object",
            "The optimizer updates the weights, not the training data.",
            "Bộ tối ưu cập nhật các trọng số, không phải dữ liệu huấn luyện.",
            "Bộ tối ưu cập nhật dữ liệu huấn luyện, không phải các trọng số.",
            "bad_semantic",
            "object relation reversed",
        ),
        pair(
            "negation_elementwise",
            "Matrix multiplication is not the same as elementwise multiplication.",
            "Phép nhân ma trận không giống với phép nhân theo từng phần tử.",
            "Phép nhân ma trận giống với phép nhân theo từng phần tử.",
            "bad_negation",
            "negation dropped",
        ),
        pair(
            "loss_direction",
            "Gradient descent minimizes the loss function.",
            "Hạ gradient tối thiểu hóa hàm mất mát.",
            "Hạ gradient tối đa hóa hàm mất mát.",
            "bad_semantic",
            "minimize vs maximize",
        ),
        pair(
            "dataset_split",
            "We tune hyperparameters on the validation set, not on the test set.",
            "Chúng ta điều chỉnh siêu tham số trên tập xác thực, không phải trên tập kiểm tra.",
            "Chúng ta điều chỉnh siêu tham số trên tập kiểm tra, không phải trên tập xác thực.",
            "bad_semantic",
            "validation/test set swapped",
        ),
        pair(
            "learning_rate_number",
            "The learning rate is reduced from 0.1 to 0.01.",
            "Tốc độ học được giảm từ 0,1 xuống 0,01.",
            "Tốc độ học được tăng từ 0,01 lên 0,1.",
            "bad_numeric",
            "number and direction reversed",
        ),
        pair(
            "variance_std",
            "The variance is the square of the standard deviation.",
            "Phương sai là bình phương của độ lệch chuẩn.",
            "Độ lệch chuẩn là bình phương của phương sai.",
            "bad_semantic",
            "variance/std relation reversed",
        ),
        pair(
            "underfit_overfit",
            "A model that underfits has high training error and high validation error.",
            "Một mô hình bị thiếu khớp có lỗi huấn luyện cao và lỗi xác thực cao.",
            "Một mô hình bị quá khớp có lỗi huấn luyện thấp và lỗi xác thực cao.",
            "bad_term",
            "underfitting changed to overfitting",
        ),
        pair(
            "softmax_sum",
            "The softmax outputs probabilities that sum to one.",
            "Softmax xuất ra các xác suất có tổng bằng một.",
            "Softmax xuất ra các xác suất không có tổng bằng một.",
            "bad_negation",
            "negation inserted",
        ),
        pair(
            "eigenvalue",
            "An eigenvalue tells how much an eigenvector is scaled.",
            "Một trị riêng cho biết một vectơ riêng được co giãn bao nhiêu.",
            "Một giá trị ví dụ cho biết một vectơ được sắp xếp như thế nào.",
            "bad_term",
            "eigenvalue/eigenvector mistranslated",
        ),
        pair(
            "convolution_kernel",
            "A convolution kernel slides across the image to detect local patterns.",
            "Một kernel tích chập trượt trên ảnh để phát hiện các mẫu cục bộ.",
            "Một hạt nhân hội tụ trượt trên ảnh để phát hiện các mẫu cục bộ.",
            "bad_term",
            "convolution kernel mistranslated",
        ),
        pair(
            "batch_epoch",
            "An epoch is complete after the model has seen every training example once.",
            "Một epoch hoàn tất sau khi mô hình đã thấy mỗi ví dụ huấn luyện một lần.",
            "Một batch hoàn tất sau khi mô hình đã thấy mỗi ví dụ huấn luyện một lần.",
            "bad_term",
            "epoch changed to batch",
        ),
        pair(
            "dimensionality_reduction",
            "Dimensionality reduction projects high-dimensional data into a lower-dimensional space.",
            "Giảm chiều chiếu dữ liệu chiều cao vào một không gian chiều thấp hơn.",
            "Chuẩn hóa dữ liệu chiều cao vào một không gian lớn hơn.",
            "bad_term",
            "dimensionality reduction mistranslated",
        ),
        pair(
            "probability_range",
            "A probability must lie between 0 and 1.",
            "Một xác suất phải nằm giữa 0 và 1.",
            "Một xác suất phải lớn hơn 1.",
            "bad_numeric",
            "probability range violated",
        ),
        pair(
            "bias_weight",
            "The bias term shifts the activation before applying the nonlinearity.",
            "Hạng bias dịch chuyển kích hoạt trước khi áp dụng phi tuyến.",
            "Trọng số dịch chuyển kích hoạt sau khi áp dụng phi tuyến.",
            "bad_semantic",
            "bias/weight and before/after changed",
        ),
        pair(
            "distribution",
            "A Gaussian distribution is fully specified by its mean and variance.",
            "Một phân phối Gaussian được xác định đầy đủ bởi trung bình và phương sai.",
            "Một phân phối đều được xác định đầy đủ bởi trung vị và độ lệch chuẩn.",
            "bad_term",
            "distribution and statistics changed",
        ),
    ]

    cases: list[dict[str, str]] = [
        case(
            "good_gradient",
            "The gradient is computed during backpropagation to update the model parameters.",
            "Gradient được tính trong quá trình lan truyền ngược để cập nhật các tham số của mô hình.",
            "good",
            "standalone good technical translation",
        )
    ]
    pairs: list[tuple[str, str]] = []
    for pair_cases, pair_ids in specs:
        cases.extend(pair_cases)
        pairs.append(pair_ids)
    return cases, pairs


def main() -> int:
    if PROBE_SET == "wide":
        cases, pairs = build_wide_probe()
    elif PROBE_SET == "basic":
        cases, pairs = build_basic_probe()
    else:
        raise SystemExit(f"Unknown COMET_PROBE_SET={PROBE_SET!r}; expected basic or wide")

    download_started = time.perf_counter()
    checkpoint = download_model(MODEL_NAME)
    download_elapsed = time.perf_counter() - download_started

    load_started = time.perf_counter()
    model = load_from_checkpoint(checkpoint)
    load_elapsed = time.perf_counter() - load_started

    data = [{"src": item["src"], "mt": item["mt"]} for item in cases]

    model.predict([data[0]], batch_size=1, gpus=0, progress_bar=False)
    started = time.perf_counter()
    output = model.predict(data, batch_size=4, gpus=0, progress_bar=False)
    elapsed = time.perf_counter() - started
    scores = [float(score) for score in output.scores]
    for item, score in zip(cases, scores):
        item["score"] = score

    throughput_data = (data * ((60 // len(data)) + 1))[:60]
    throughput_started = time.perf_counter()
    model.predict(throughput_data, batch_size=8, gpus=0, progress_bar=False)
    throughput_elapsed = time.perf_counter() - throughput_started

    by_id = {item["id"]: item for item in cases}
    pair_results = []
    for good_id, bad_id in pairs:
        good_score = by_id[good_id]["score"]
        bad_score = by_id[bad_id]["score"]
        pair_results.append(
            {
                "good": good_id,
                "bad": bad_id,
                "good_score": good_score,
                "bad_score": bad_score,
                "ok": good_score > bad_score,
                "margin": good_score - bad_score,
            }
        )

    payload = {
        "model": MODEL_NAME,
        "probe_set": PROBE_SET,
        "device": "cpu",
        "torch": {
            "version": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
        },
        "download_elapsed_sec": download_elapsed,
        "load_elapsed_sec": load_elapsed,
        "checkpoint": str(checkpoint),
        "unique_cases": len(cases),
        "unique_elapsed_sec": elapsed,
        "unique_items_per_sec": len(cases) / elapsed,
        "throughput_items": len(throughput_data),
        "throughput_elapsed_sec": throughput_elapsed,
        "throughput_items_per_sec": len(throughput_data) / throughput_elapsed,
        "score_summary": {
            "min": min(scores),
            "max": max(scores),
            "mean": statistics.mean(scores),
        },
        "pairwise_expected_order_ok": sum(item["ok"] for item in pair_results),
        "pairwise_expected_order_total": len(pair_results),
        "pair_results": pair_results,
        "cases": cases,
    }
    safe_name = OUT_NAME or MODEL_NAME.replace("/", "_").replace("-", "_").lower()
    out_path = pathlib.Path(f"data/reports/comet_qe_probe_{safe_name}_{PROBE_SET}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
