# PHIEU CHAM MU PJ — 40 cap (calibration §4i)

**Cach cham:** voi moi cau, doc EN goc roi doc Ban X va Ban Y. Dien 2 dong:
- `Overall:` ban dich nao tot hon TONG THE (X / Y / HOA)
- `Style:` ban nao doc TU NHIEN hon, bo qua khac biet thuat ngu/nghia (X / Y / HOA)

Cham theo cam nhan doc gia — khong tra tu dien, khong doi chieu gold, khong xem file khac. HOA la dap an hop le, dung ep minh chon.

---

## Cau 1

**EN:**
```
One nice sanity check is to see
whether the predictions on the test set
resemble those of the $K$-fold cross-validation process.
If they do, it is time to upload them to Kaggle.
The following code will generate a file called `submission.csv`.
```
**Ban X:**
```
Một phép kiểm tra hợp lý hữu ích là xem các dự đoán trên tập kiểm tra có giống với các dự đoán từ quá trình kiểm định chéo $K$-fold hay không. Nếu có, đã đến lúc tải chúng lên Kaggle. Đoạn mã sau sẽ tạo ra một tệp có tên `submission.csv`.
```
**Ban Y:**
```
Một phép kiểm tra hợp lý hữu ích là xem liệu các dự đoán trên tập kiểm tra có giống với những gì thu được từ quá trình xác thực chéo $K$-fold hay không. Nếu có, đã đến lúc tải chúng lên Kaggle. Đoạn mã sau sẽ tạo ra một tệp có tên `submission.csv`.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 2

**EN:**
```
## [**Model Selection**]
```
**Ban X:**
```
## [**Lựa chọn mô hình**]
```
**Ban Y:**
```
## [**Chọn mô hình**]
```
- Overall: X
- Style: X

---

## Cau 3

**EN:**
```
We can view the equivalence formally
by proving that for any values of the weights,
we can just collapse out the hidden layer,
yielding an equivalent single-layer model with parameters
$\mathbf{W} = \mathbf{W}^{(1)}\mathbf{W}^{(2)}$ and $\mathbf{b} = \mathbf{b}^{(1)} \mathbf{W}^{(2)} + \mathbf{b}^{(2)}$:
```
**Ban X:**
```
Ta có thể xem sự tương đương này một cách hình thức bằng cách chứng minh rằng với mọi giá trị của các trọng số, ta chỉ cần khử đi tầng ẩn, thu được một mô hình một tầng tương đương với các tham số $\mathbf{W} = \mathbf{W}^{(1)}\mathbf{W}^{(2)}$ và $\mathbf{b} = \mathbf{b}^{(1)} \mathbf{W}^{(2)} + \mathbf{b}^{(2)}$:
```
**Ban Y:**
```
Ta có thể xem sự tương đương này một cách hình thức bằng cách chứng minh rằng với mọi giá trị của các trọng số, ta chỉ cần gộp bỏ tầng ẩn, thu được một mô hình một tầng tương đương với các tham số $\mathbf{W} = \mathbf{W}^{(1)}\mathbf{W}^{(2)}$ và $\mathbf{b} = \mathbf{b}^{(1)} \mathbf{W}^{(2)} + \mathbf{b}^{(2)}$:
```
- Overall: X
- Style: X

---

## Cau 4

**EN:**
```
In standard dropout regularization,
one debiases each layer by normalizing
by the fraction of nodes that were retained (not dropped out).
In other words,
with *dropout probability* $p$,
each intermediate activation $h$ is replaced by
a random variable $h'$ as follows:
```
**Ban X:**
```
Trong chuẩn hóa dropout tiêu chuẩn, ta khử sai lệch cho mỗi lớp bằng cách chuẩn hóa theo phần trăm các nút được giữ lại (không bị loại bỏ). Nói cách khác, với *xác suất dropout* $p$, mỗi kích hoạt trung gian $h$ được thay thế bởi một biến ngẫu nhiên $h'$ như sau:
```
**Ban Y:**
```
Trong chuẩn hóa dropout tiêu chuẩn, ta khử sai lệch cho từng tầng bằng cách chuẩn hóa theo tỉ lệ các nút được giữ lại (không bị dropout). Nói cách khác, với *xác suất dropout* $p$, mỗi kích hoạt trung gian $h$ được thay thế bởi một biến ngẫu nhiên $h'$ như sau:
```
- Overall: HÒA
- Style: X

---

## Cau 5

**EN:**
```
To begin, we stick with the passive prediction setting
considering the various ways that data distributions might shift
and what might be done to salvage model performance.
In one classic setup, we assume that our training data
were sampled from some distribution $p_S(\mathbf{x},y)$
but that our test data will consist
of unlabeled examples drawn from
some different distribution $p_T(\mathbf{x},y)$.
Already, we must confront a sobering reality.
Absent any assumptions on how $p_S$
and $p_T$ relate to each other,
learning a robust classifier is impossible.
```
**Ban X:**
```
Để bắt đầu, chúng ta vẫn ở trong thiết lập dự đoán thụ động, xem xét các cách khác nhau mà phân phối dữ liệu có thể thay đổi và những gì có thể làm để cứu vãn hiệu năng của mô hình. Trong một thiết lập kinh điển, ta giả sử rằng dữ liệu huấn luyện của mình được lấy mẫu từ một phân phối nào đó $p_S(\mathbf{x},y)$ nhưng dữ liệu kiểm tra sẽ gồm các ví dụ chưa gán nhãn được rút ra từ một phân phối khác $p_T(\mathbf{x},y)$. Ngay từ đây, chúng ta phải đối mặt với một thực tế đáng suy ngẫm. Nếu không có bất kỳ giả định nào về mối quan hệ giữa $p_S$ và $p_T$, thì việc học một bộ phân loại vững chắc là không thể.
```
**Ban Y:**
```
Để bắt đầu, chúng ta sẽ giữ nguyên thiết lập dự đoán thụ động, xem xét các cách khác nhau mà phân phối dữ liệu có thể thay đổi và những gì có thể làm để cứu vãn hiệu năng của mô hình. Trong một thiết lập kinh điển, chúng ta giả sử rằng dữ liệu huấn luyện của mình được lấy mẫu từ một phân phối nào đó $p_S(\mathbf{x},y)$ nhưng dữ liệu kiểm tra sẽ gồm các ví dụ chưa gán nhãn được rút ra từ một phân phối khác $p_T(\mathbf{x},y)$. Ngay từ đầu, chúng ta phải đối mặt với một thực tế đáng suy ngẫm. Nếu không có bất kỳ giả định nào về mối quan hệ giữa $p_S$ và $p_T$, thì việc học một bộ phân loại vững chắc là không thể.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 6

**EN:**
```
### Bandits
```
**Ban X:**
```
### Bandit
```
**Ban Y:**
```
### Bài toán bandit
```
- Overall: Y
- Style: Y

---

## Cau 7

**EN:**
```
The noise term $\epsilon$ obeys a normal distribution
with a mean of 0 and a standard deviation of 0.1.
For optimization, we typically want to avoid
very large values of gradients or losses.
This is why the *features*
are rescaled from $x^i$ to $\frac{x^i}{i!}$.
It allows us to avoid very large values for large exponents $i$.
We will synthesize 100 samples each for the training set and test set.
```
**Ban X:**
```
Hạng nhiễu $\epsilon$ tuân theo phân phối chuẩn với kỳ vọng bằng 0 và độ lệch chuẩn bằng 0.1. Để tối ưu hóa, ta thường muốn tránh các giá trị quá lớn của gradient hoặc loss. Đây là lý do *đặc trưng* được chuẩn hóa lại từ $x^i$ thành $\frac{x^i}{i!}$. Điều này cho phép ta tránh các giá trị quá lớn khi số mũ $i$ lớn. Chúng ta sẽ tạo ngẫu nhiên 100 mẫu cho mỗi tập huấn luyện và kiểm tra.
```
**Ban Y:**
```
Thành phần nhiễu $\epsilon$ tuân theo một phân phối chuẩn với trung bình bằng 0 và độ lệch chuẩn bằng 0.1. Để tối ưu hóa, chúng ta thường muốn tránh các giá trị quá lớn của gradient hoặc loss. Đây là lý do các *đặc trưng* được tái chuẩn hóa từ $x^i$ thành $\frac{x^i}{i!}$. Điều này cho phép chúng ta tránh các giá trị quá lớn khi số mũ $i$ lớn. Chúng ta sẽ tổng hợp 100 mẫu cho mỗi tập huấn luyện và tập kiểm tra.
```
- Overall: Y
- Style: Y

---

## Cau 8

**EN:**
```
Now we are able to calculate the gradient
$\partial J/\partial \mathbf{W}^{(2)} \in \mathbb{R}^{q \times h}$
of the model parameters closest to the output layer.
Using the chain rule yields:
```
**Ban X:**
```
Bây giờ chúng ta có thể tính gradient $\partial J/\partial \mathbf{W}^{(2)} \in \mathbb{R}^{q \times h}$ của các tham số mô hình gần lớp đầu ra nhất. Áp dụng quy tắc dây chuyền cho ta:
```
**Ban Y:**
```
Bây giờ chúng ta có thể tính gradient $\partial J/\partial \mathbf{W}^{(2)} \in \mathbb{R}^{q \times h}$ của các tham số mô hình gần tầng đầu ra nhất. Áp dụng quy tắc chuỗi cho ta:
```
- Overall: Y
- Style: HÒA

---

## Cau 9

**EN:**
```
Alas, we do not know that ratio,
so before we can do anything useful we need to estimate it.
Many methods are available,
including some fancy operator-theoretic approaches
that attempt to recalibrate the expectation operator directly
using a minimum-norm or a maximum entropy principle.
Note that for any such approach, we need samples
drawn from both distributions---the "true" $p$, e.g.,
by access to test data, and the one used
for generating the training set $q$ (the latter is trivially available).
Note however, that we only need features $\mathbf{x} \sim p(\mathbf{x})$;
we do not need to access labels $y \sim p(y)$.
```
**Ban X:**
```
Than ôi, chúng ta không biết tỷ số đó, nên trước khi có thể làm bất cứ điều gì hữu ích, ta cần ước lượng nó. Có nhiều phương pháp khả dụng, bao gồm một số cách tiếp cận theo toán tử khá tinh vi nhằm hiệu chỉnh lại trực tiếp toán tử kỳ vọng bằng nguyên lý chuẩn tối thiểu hoặc nguyên lý entropy cực đại. Lưu ý rằng với bất kỳ cách tiếp cận nào như vậy, ta cần các mẫu được lấy từ cả hai phân phối---phân phối "thực" $p$, ví dụ bằng cách truy cập dữ liệu kiểm tra, và phân phối được dùng để tạo tập huấn luyện $q$ (phân phối sau thì hiển nhiên có sẵn). Tuy nhiên, lưu ý rằng ta chỉ cần các đặc trưng $\mathbf{x} \sim p(\mathbf{x})$; ta không cần truy cập các nhãn $y \sim p(y)$.
```
**Ban Y:**
```
Than ôi, chúng ta không biết tỷ số đó, vì vậy trước khi có thể làm bất cứ điều gì hữu ích, chúng ta cần ước lượng nó. Có nhiều phương pháp khả dụng, bao gồm một số cách tiếp cận lý thuyết toán tử cầu kỳ cố gắng hiệu chỉnh lại trực tiếp toán tử kỳ vọng bằng cách sử dụng chuẩn tối thiểu hoặc nguyên lý entropy cực đại. Lưu ý rằng với bất kỳ cách tiếp cận nào như vậy, chúng ta cần các mẫu được lấy từ cả hai phân phối---phân phối "thật" $p$, ví dụ, bằng cách truy cập dữ liệu kiểm tra, và phân phối được dùng để tạo tập huấn luyện $q$ (phân phối sau thì hiển nhiên có sẵn). Tuy nhiên, lưu ý rằng chúng ta chỉ cần các đặc trưng $\mathbf{x} \sim p(\mathbf{x})$; chúng ta không cần truy cập các nhãn $y \sim p(y)$.
```
- Overall: X
- Style: X

---

## Cau 10

**EN:**
```
Moreover, you might ask why we work with the $L_2$ norm
in the first place and not, say, the $L_1$ norm.
In fact, other choices are valid and
popular throughout statistics.
While $L_2$-regularized linear models constitute
the classic *ridge regression* algorithm,
$L_1$-regularized linear regression
is a similarly fundamental model in statistics, which is popularly known as *lasso regression*.
```
**Ban X:**
```
Hơn nữa, bạn có thể hỏi vì sao ngay từ đầu chúng ta lại làm việc với chuẩn $L_2$ chứ không phải, chẳng hạn, chuẩn $L_1$. Thực ra, các lựa chọn khác cũng hợp lệ và phổ biến trong thống kê. Trong khi các mô hình tuyến tính được regularization bằng $L_2$ tạo thành thuật toán cổ điển *ridge regression*, hồi quy tuyến tính được regularization bằng $L_1$ là một mô hình nền tảng tương tự trong thống kê, thường được gọi là *lasso regression*.
```
**Ban Y:**
```
Hơn nữa, bạn có thể hỏi vì sao ngay từ đầu chúng ta làm việc với chuẩn $L_2$ chứ không phải, chẳng hạn, chuẩn $L_1$. Thực ra, các lựa chọn khác cũng hợp lệ và phổ biến trong thống kê. Trong khi các mô hình tuyến tính được chuẩn hóa $L_2$ tạo thành thuật toán *hồi quy ridge* kinh điển, hồi quy tuyến tính được chuẩn hóa $L_1$ là một mô hình nền tảng tương tự trong thống kê, thường được biết đến với tên *hồi quy lasso*.
```
- Overall: Y
- Style: Y

---

## Cau 11

**EN:**
```
We plot the tanh function below.
Note that as the input nears 0, the tanh function approaches a linear transformation. Although the shape of the function is similar to that of the sigmoid function, the tanh function exhibits point symmetry about the origin of the coordinate system.
```
**Ban X:**
```
Chúng ta vẽ hàm tanh dưới đây. Lưu ý rằng khi đầu vào tiến gần 0, hàm tanh tiến gần một phép biến đổi tuyến tính. Mặc dù hình dạng của hàm tương tự như hàm sigmoid, hàm tanh thể hiện đối xứng điểm qua gốc của hệ tọa độ.
```
**Ban Y:**
```
Chúng ta vẽ hàm tanh bên dưới. Lưu ý rằng khi đầu vào tiến gần 0, hàm tanh tiến gần một biến đổi tuyến tính. Mặc dù hình dạng của hàm tương tự như hàm sigmoid, hàm tanh thể hiện đối xứng điểm qua gốc của hệ tọa độ.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 12

**EN:**
```
## High-Dimensional Linear Regression
```
**Ban X:**
```
## Hồi quy tuyến tính chiều cao
```
**Ban Y:**
```
## Hồi quy tuyến tính nhiều chiều
```
- Overall: Y
- Style: Y

---

## Cau 13

**EN:**
```
# Multilayer Perceptrons
:label:`chap_perceptrons`
```
**Ban X:**
```
# Mạng nơ-ron nhiều lớp :label:`chap_perceptrons`
```
**Ban Y:**
```
# Perceptron đa tầng :label:`chap_perceptrons`
```
- Overall: HÒA
- Style: HÒA

---

## Cau 14

**EN:**
```
To get started we train a linear model with squared loss.
Not surprisingly, our linear model will not lead
to a competition-winning submission
but it provides a sanity check to see whether
there is meaningful information in the data.
If we cannot do better than random guessing here,
then there might be a good chance
that we have a data processing bug.
And if things work, the linear model will serve as a baseline
giving us some intuition about how close the simple model
gets to the best reported models, giving us a sense
of how much gain we should expect from fancier models.
```
**Ban X:**
```
Để bắt đầu, chúng ta huấn luyện một mô hình tuyến tính với hàm mất mát bình phương. Không có gì ngạc nhiên, mô hình tuyến tính của chúng ta sẽ không dẫn đến một bài nộp chiến thắng trong cuộc thi, nhưng nó cung cấp một phép kiểm tra hợp lý để xem liệu dữ liệu có chứa thông tin hữu ích hay không. Nếu ở đây chúng ta không thể làm tốt hơn đoán ngẫu nhiên, thì rất có thể chúng ta đã mắc lỗi trong quá trình xử lý dữ liệu. Và nếu mọi thứ hoạt động, mô hình tuyến tính sẽ đóng vai trò như một mốc cơ sở, cho chúng ta một số trực giác về việc mô hình đơn giản này tiến gần đến các mô hình tốt nhất được báo cáo đến mức nào, qua đó cho chúng ta cảm nhận về mức cải thiện mà ta nên kỳ vọng từ các mô hình tinh vi hơn.
```
**Ban Y:**
```
Để bắt đầu, chúng ta huấn luyện một mô hình tuyến tính với mất mát bình phương. Không có gì ngạc nhiên, mô hình tuyến tính của chúng ta sẽ không mang lại một bài nộp chiến thắng trong cuộc thi, nhưng nó cung cấp một kiểm tra hợp lý để xem liệu có thông tin có ý nghĩa trong dữ liệu hay không. Nếu ở đây chúng ta không thể làm tốt hơn đoán ngẫu nhiên, thì rất có thể chúng ta đã mắc lỗi trong xử lý dữ liệu. Và nếu mọi thứ hoạt động, mô hình tuyến tính sẽ đóng vai trò như một mốc chuẩn, cho chúng ta một số trực giác về việc mô hình đơn giản này tiến gần đến các mô hình được báo cáo tốt nhất đến mức nào, qua đó cho chúng ta cảm nhận về mức cải thiện mà chúng ta nên kỳ vọng từ các mô hình tinh vi hơn.
```
- Overall: X
- Style: X

---

## Cau 15

**EN:**
```
### Incorporating Hidden Layers
```
**Ban X:**
```
### Kết hợp các tầng ẩn
```
**Ban Y:**
```
### Kết hợp các lớp ẩn
```
- Overall: HÒA
- Style: HÒA

---

## Cau 16

**EN:**
```
Finally, we can obtain the gradient
$\partial J/\partial \mathbf{W}^{(1)} \in \mathbb{R}^{h \times d}$
of the model parameters closest to the input layer.
According to the chain rule, we get
```
**Ban X:**
```
Cuối cùng, chúng ta có thể thu được gradient $\partial J/\partial \mathbf{W}^{(1)} \in \mathbb{R}^{h \times d}$ của các tham số mô hình gần lớp đầu vào nhất. Theo quy tắc dây chuyền, ta có
```
**Ban Y:**
```
Cuối cùng, chúng ta có thể thu được gradient $\partial J/\partial \mathbf{W}^{(1)} \in \mathbb{R}^{h \times d}$ của các tham số mô hình gần tầng đầu vào nhất. Theo quy tắc chuỗi, ta có
```
- Overall: Y
- Style: HÒA

---

## Cau 17

**EN:**
```
Activation functions decide whether a neuron should be activated or not by
calculating the weighted sum and further adding bias with it.
They are differentiable operators to transform input signals to outputs,
while most of them add non-linearity.
Because activation functions are fundamental to deep learning,
(**let us briefly survey some common activation functions**).
```
**Ban X:**
```
Các hàm kích hoạt quyết định một nơ-ron có nên được kích hoạt hay không bằng cách tính tổng có trọng số và cộng thêm độ lệch vào đó. Chúng là các toán tử khả vi để biến đổi tín hiệu đầu vào thành đầu ra, trong khi hầu hết chúng đều thêm tính phi tuyến. Vì các hàm kích hoạt là nền tảng của học sâu, (**chúng ta hãy lược khảo ngắn gọn một số hàm kích hoạt phổ biến**).
```
**Ban Y:**
```
Các hàm kích hoạt quyết định liệu một nơ-ron có nên được kích hoạt hay không bằng cách tính tổng có trọng số rồi cộng thêm độ lệch vào đó. Chúng là các toán tử khả vi để biến đổi tín hiệu đầu vào thành đầu ra, trong khi phần lớn chúng thêm phi tuyến. Vì các hàm kích hoạt là nền tảng của học sâu, (**hãy cùng lướt qua một số hàm kích hoạt phổ biến**).
```
- Overall: HÒA
- Style: Y

---

## Cau 18

**EN:**
```
1. Try adding different numbers of hidden layers (you may also modify the learning rate). What setting works best?
1. Try out different activation functions. Which one works best?
1. Try different schemes for initializing the weights. What method works best?
```
**Ban X:**
```
1. Hãy thử thêm các số lượng tầng ẩn khác nhau (bạn cũng có thể sửa đổi tốc độ học). Thiết lập nào hoạt động tốt nhất? 1. Hãy thử các hàm kích hoạt khác nhau. Hàm nào hoạt động tốt nhất? 1. Hãy thử các sơ đồ khác nhau để khởi tạo các trọng số. Phương pháp nào hoạt động tốt nhất?
```
**Ban Y:**
```
1. Thử thêm các số lượng lớp ẩn khác nhau (bạn cũng có thể sửa đổi tốc độ học). Thiết lập nào hoạt động tốt nhất? 1. Thử các hàm kích hoạt khác nhau. Hàm nào hoạt động tốt nhất? 1. Thử các lược đồ khác nhau để khởi tạo trọng số. Phương pháp nào hoạt động tốt nhất?
```
- Overall: HÒA
- Style: HÒA

---

## Cau 19

**EN:**
```
As stated above, we have a wide variety of data types.
We will need to preprocess the data before we can start modeling.
Let us start with the numerical features.
First, we apply a heuristic,
[**replacing all missing values
by the corresponding feature's mean.**]
Then, to put all features on a common scale,
we (***standardize* the data by
rescaling features to zero mean and unit variance**):
```
**Ban X:**
```
Như đã nêu ở trên, chúng ta có rất nhiều loại dữ liệu khác nhau. Chúng ta sẽ cần tiền xử lý dữ liệu trước khi có thể bắt đầu xây dựng mô hình. Hãy bắt đầu với các đặc trưng số. Trước hết, chúng ta áp dụng một heuristic, [**thay thế tất cả các giá trị khuyết bằng trung bình của đặc trưng tương ứng.**] Sau đó, để đưa tất cả các đặc trưng về cùng một thang đo, chúng ta (***chuẩn hóa* dữ liệu bằng cách tái tỷ lệ các đặc trưng về trung bình bằng 0 và phương sai đơn vị**):
```
**Ban Y:**
```
Như đã nêu ở trên, chúng ta có rất nhiều loại dữ liệu khác nhau. Trước khi có thể bắt đầu mô hình hóa, chúng ta cần tiền xử lý dữ liệu. Hãy bắt đầu với các đặc trưng số. Trước hết, ta áp dụng một quy tắc kinh nghiệm, [**thay thế tất cả các giá trị thiếu bằng giá trị trung bình của đặc trưng tương ứng.**] Sau đó, để đưa tất cả các đặc trưng về cùng một thang đo, ta (***chuẩn hóa* dữ liệu bằng cách tái tỷ lệ các đặc trưng về trung bình bằng 0 và phương sai đơn vị**):
```
- Overall: Y
- Style: Y

---

## Cau 20

**EN:**
```
To make sure we know how everything works,
we will [**implement the ReLU activation**] ourselves
using the maximum function rather than
invoking the built-in `relu` function directly.
```
**Ban X:**
```
Để chắc chắn rằng chúng ta biết mọi thứ hoạt động như thế nào, chúng ta sẽ [**tự triển khai hàm kích hoạt ReLU**] bằng cách sử dụng hàm maximum thay vì gọi trực tiếp hàm `relu` có sẵn.
```
**Ban Y:**
```
Để chắc chắn rằng chúng ta biết mọi thứ hoạt động như thế nào, chúng ta sẽ [**tự mình triển khai kích hoạt ReLU**] bằng cách sử dụng hàm lớn nhất thay vì gọi trực tiếp hàm `relu` dựng sẵn.
```
- Overall: HÒA
- Style: X

---

## Cau 21

**EN:**
```
Note that we can easily come up with examples
that violate monotonicity.
Say for example that we want to predict probability
of death based on body temperature.
For individuals with a body temperature
above 37°C (98.6°F),
higher temperatures indicate greater risk.
However, for individuals with body temperatures
below 37° C, higher temperatures indicate lower risk!
In this case too, we might resolve the problem
with some clever preprocessing.
Namely, we might use the distance from 37°C as our feature.
```
**Ban X:**
```
Lưu ý rằng chúng ta có thể dễ dàng nghĩ ra các ví dụ vi phạm tính đơn điệu. Chẳng hạn, giả sử rằng chúng ta muốn dự đoán xác suất tử vong dựa trên nhiệt độ cơ thể. Với những người có nhiệt độ cơ thể trên 37°C (98.6°F), nhiệt độ cao hơn cho thấy rủi ro lớn hơn. Tuy nhiên, với những người có nhiệt độ cơ thể dưới 37°C, nhiệt độ cao hơn lại cho thấy rủi ro thấp hơn! Trong trường hợp này, chúng ta cũng có thể giải quyết vấn đề bằng một bước tiền xử lý khéo léo nào đó. Cụ thể, chúng ta có thể dùng khoảng cách đến 37°C làm đặc trưng của mình.
```
**Ban Y:**
```
Lưu ý rằng chúng ta có thể dễ dàng nghĩ ra các ví dụ vi phạm tính đơn điệu. Chẳng hạn, giả sử chúng ta muốn dự đoán xác suất tử vong dựa trên nhiệt độ cơ thể. Với những người có nhiệt độ cơ thể trên 37°C (98.6°F), nhiệt độ cao hơn cho thấy rủi ro lớn hơn. Tuy nhiên, với những người có nhiệt độ cơ thể dưới 37°C, nhiệt độ cao hơn lại cho thấy rủi ro thấp hơn! Trong trường hợp này, chúng ta cũng có thể giải quyết vấn đề bằng một bước tiền xử lý khéo léo nào đó. Cụ thể, chúng ta có thể dùng khoảng cách tới 37°C làm đặc trưng.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 22

**EN:**
```
$$L(\mathbf{w}, b) = \frac{1}{n}\sum_{i=1}^n \frac{1}{2}\left(\mathbf{w}^\top \mathbf{x}^{(i)} + b - y^{(i)}\right)^2.$$
```
**Ban X:**
```
$$L([1mw[0m, b) = \frac{1}{n}\sum_{i=1}^n \frac{1}{2}\left(\mathbf{w}^\top \mathbf{x}^{(i)} + b - y^{(i)}\right)^2.$$
```
**Ban Y:**
```
$$L(mathbf{w}, b) = \frac{1}{n}\sum_{i=1}^n \frac{1}{2}\left(\mathbf{w}^\top \mathbf{x}^{(i)} + b - y^{(i)}\right)^2.$$
```
- Overall: Y
- Style: Y

---

## Cau 23

**EN:**
```
Notice that sometimes the number of training errors
for a set of hyperparameters can be very low,
even as the number of errors on $K$-fold cross-validation
is considerably higher.
This indicates that we are overfitting.
Throughout training you will want to monitor both numbers.
Less overfitting might indicate that our data can support a more powerful model.
Massive overfitting might suggest that we can gain
by incorporating regularization techniques.
```
**Ban X:**
```
Lưu ý rằng đôi khi số lỗi huấn luyện cho một bộ siêu tham số có thể rất thấp, ngay cả khi số lỗi trên xác thực chéo $K$-fold lại cao hơn đáng kể. Điều này cho thấy rằng chúng ta đang quá khớp. Trong suốt quá trình training, bạn sẽ muốn theo dõi cả hai con số. Ít quá khớp hơn có thể cho thấy rằng dữ liệu của chúng ta có thể hỗ trợ một mô hình mạnh hơn. Quá khớp nghiêm trọng có thể gợi ý rằng chúng ta có thể cải thiện bằng cách đưa vào các kỹ thuật chuẩn hóa.
```
**Ban Y:**
```
Lưu ý rằng đôi khi số lỗi huấn luyện cho một bộ siêu tham số có thể rất thấp, ngay cả khi số lỗi trên kiểm định chéo $K$-fold lại cao hơn đáng kể. Điều này cho thấy chúng ta đang bị quá khớp. Trong suốt quá trình huấn luyện, bạn sẽ muốn theo dõi cả hai con số này. Ít quá khớp hơn có thể cho thấy dữ liệu của chúng ta có thể hỗ trợ một mô hình mạnh hơn. Quá khớp nghiêm trọng có thể gợi ý rằng chúng ta có thể cải thiện bằng cách đưa vào các kỹ thuật chính quy hóa.
```
- Overall: Y
- Style: Y

---

## Cau 24

**EN:**
```
As you can see, (**the sigmoid's gradient vanishes
both when its inputs are large and when they are small**).
Moreover, when backpropagating through many layers,
unless we are in the Goldilocks zone, where
the inputs to many of the sigmoids are close to zero,
the gradients of the overall product may vanish.
When our network boasts many layers,
unless we are careful, the gradient
will likely be cut off at some layer.
Indeed, this problem used to plague deep network training.
Consequently, ReLUs, which are more stable
(but less neurally plausible),
have emerged as the default choice for practitioners.
```
**Ban X:**
```
Như bạn có thể thấy, (**gradient của hàm sigmoid biến mất cả khi đầu vào của nó lớn lẫn khi chúng nhỏ**). Hơn nữa, khi lan truyền ngược qua nhiều tầng, trừ khi chúng ta ở trong Goldilocks zone, nơi đầu vào của nhiều hàm sigmoid gần bằng không, gradient của tích tổng thể có thể biến mất. Khi mạng của chúng ta có nhiều tầng, nếu không cẩn thận, gradient rất có thể sẽ bị cắt đứt ở một tầng nào đó. Thật vậy, vấn đề này từng gây khó khăn cho training mạng sâu. Do đó, ReLU, vốn ổn định hơn (nhưng kém phù hợp với sinh học thần kinh hơn), đã trở thành lựa chọn mặc định cho các nhà thực hành.
```
**Ban Y:**
```
Như bạn có thể thấy, (**gradient của sigmoid biến mất cả khi đầu vào của nó lớn lẫn khi chúng nhỏ**). Hơn nữa, khi lan truyền ngược qua nhiều tầng, trừ khi chúng ta ở trong vùng Goldilocks, nơi đầu vào của nhiều sigmoid gần bằng không, các gradient của tích tổng thể có thể biến mất. Khi mạng của chúng ta có nhiều tầng, nếu không cẩn thận, gradient rất có thể sẽ bị cắt đứt ở một tầng nào đó. Thật vậy, vấn đề này từng gây khó khăn cho việc huấn luyện mạng sâu. Do đó, ReLU, vốn ổn định hơn (nhưng kém phù hợp với sinh học thần kinh hơn), đã trở thành lựa chọn mặc định của các nhà thực hành.
```
- Overall: Y
- Style: Y

---

## Cau 25

**EN:**
```
Recall that $\mathbf{x}^{(i)}$ are the features,
$y^{(i)}$ are labels for all data examples $i$, and $(\mathbf{w}, b)$
are the weight and bias parameters, respectively.
To penalize the size of the weight vector,
we must somehow add $\| \mathbf{w} \|^2$ to the loss function,
but how should the model trade off the
standard loss for this new additive penalty?
In practice, we characterize this tradeoff
via the *regularization constant* $\lambda$,
a non-negative hyperparameter
that we fit using validation data:
```
**Ban X:**
```
Nhắc lại rằng $\mathbf{x}^{(i)}$ là các đặc trưng, $y^{(i)}$ là nhãn cho mọi ví dụ dữ liệu $i$, và $(\mathbf{w}, b)$ lần lượt là các tham số trọng số và độ lệch. Để phạt kích thước của vector trọng số, chúng ta phải bằng cách nào đó thêm $\| \mathbf{w} \|^2$ vào hàm mất mát, nhưng mô hình nên đánh đổi mất mát chuẩn với phạt cộng thêm mới này như thế nào? Trong thực tế, chúng ta đặc trưng hóa sự đánh đổi này thông qua *hằng số chuẩn hóa* $\lambda$, một siêu tham số không âm mà chúng ta khớp bằng tập xác thực:
```
**Ban Y:**
```
Nhắc lại rằng $\mathbf{x}^{(i)}$ là các đặc trưng, $y^{(i)}$ là nhãn cho mọi ví dụ dữ liệu $i$, và $(\mathbf{w}, b)$ lần lượt là các tham số trọng số và độ lệch. Để phạt độ lớn của vector trọng số, chúng ta phải bằng cách nào đó thêm $\| \mathbf{w} \|^2$ vào hàm mất mát, nhưng mô hình nên đánh đổi giữa mất mát chuẩn và hình phạt cộng thêm mới này như thế nào? Trong thực tế, chúng ta đặc trưng hóa sự đánh đổi này thông qua *hằng số regularization* $\lambda$, một siêu tham số không âm mà chúng ta khớp bằng dữ liệu xác thực:
```
- Overall: HÒA
- Style: Y

---

## Cau 26

**EN:**
```
It turns out that under some mild conditions---if
our classifier was reasonably accurate in the first place,
and if the target data contain only categories
that we have seen before,
and if the label shift assumption holds in the first place
(the strongest assumption here),
then we can estimate the test set label distribution
by solving a simple linear system
```
**Ban X:**
```
Hóa ra dưới một số điều kiện nhẹ---nếu bộ phân loại của chúng ta vốn đã đủ chính xác, và nếu dữ liệu đích chỉ chứa các danh mục mà chúng ta đã từng thấy trước đó, và nếu giả định label shift đúng ngay từ đầu (đây là giả định mạnh nhất ở đây), thì chúng ta có thể ước lượng phân phối nhãn của tập kiểm tra bằng cách giải một hệ tuyến tính đơn giản
```
**Ban Y:**
```
Hóa ra dưới một số điều kiện nhẹ nhàng---nếu bộ phân loại của chúng ta vốn đã khá chính xác, và nếu dữ liệu mục tiêu chỉ chứa những danh mục mà chúng ta đã từng thấy trước đây, và nếu giả định dịch chuyển nhãn đúng ngay từ đầu (đây là giả định mạnh nhất ở đây), thì chúng ta có thể ước lượng phân phối nhãn của tập kiểm tra bằng cách giải một hệ tuyến tính đơn giản
```
- Overall: HÒA
- Style: HÒA

---

## Cau 27

**EN:**
```
# Numerical Stability and Initialization
:label:`sec_numerical_stability`
```
**Ban X:**
```
# Ổn định số và khởi tạo :label:`sec_numerical_stability`
```
**Ban Y:**
```
# Ổn định số và Khởi tạo :label:`sec_numerical_stability`
```
- Overall: X
- Style: X

---

## Cau 28

**EN:**
```
*Forward propagation* (or *forward pass*) refers to the calculation and storage
of intermediate variables (including outputs)
for a neural network in order
from the input layer to the output layer.
We now work step-by-step through the mechanics
of a neural network with one hidden layer.
This may seem tedious but in the eternal words
of funk virtuoso James Brown,
you must "pay the cost to be the boss".
```
**Ban X:**
```
*Lan truyền xuôi* (hoặc *forward pass*) là việc tính toán và lưu trữ các biến trung gian (bao gồm cả đầu ra) cho một mạng nơ-ron theo thứ tự từ tầng đầu vào đến tầng đầu ra. Bây giờ chúng ta sẽ đi từng bước qua cơ chế của một mạng nơ-ron có một tầng ẩn. Điều này có vẻ tẻ nhạt nhưng, theo lời bất hủ của bậc thầy funk James Brown, bạn phải "pay the cost to be the boss".
```
**Ban Y:**
```
*Lan truyền xuôi* (hoặc *forward pass*) là việc tính toán và lưu trữ các biến trung gian (bao gồm cả đầu ra) cho một mạng nơ-ron theo thứ tự từ tầng đầu vào đến tầng đầu ra. Bây giờ chúng ta sẽ đi từng bước qua cơ chế của một mạng nơ-ron với một tầng ẩn. Điều này có vẻ tẻ nhạt nhưng theo lời bất hủ của bậc thầy funk James Brown, bạn phải "pay the cost to be the boss".
```
- Overall: X
- Style: X

---

## Cau 29

**EN:**
```
Though the assumption for nonexistence of nonlinearities
in the above mathematical reasoning
can be easily violated in neural networks,
the Xavier initialization method
turns out to work well in practice.
```
**Ban X:**
```
Mặc dù giả định về việc không tồn tại phi tuyến trong lập luận toán học ở trên có thể dễ dàng bị vi phạm trong mạng nơ-ron, phương pháp khởi tạo Xavier hóa ra hoạt động tốt trong thực tế.
```
**Ban Y:**
```
Mặc dù giả định về sự không tồn tại của các phi tuyến trong lập luận toán học ở trên có thể dễ dàng bị vi phạm trong các mạng nơ-ron, phương pháp khởi tạo Xavier hóa ra hoạt động tốt trong thực tế.
```
- Overall: X
- Style: X

---

## Cau 30

**EN:**
```
The training set consists of photos,
while the test set contains only cartoons.
Training on a dataset with substantially different
characteristics from the test set
can spell trouble absent a coherent plan
for how to adapt to the new domain.
```
**Ban X:**
```
Tập huấn luyện gồm các bức ảnh, trong khi tập kiểm tra chỉ chứa tranh hoạt hình. Việc huấn luyện trên một bộ dữ liệu có đặc tính khác biệt đáng kể so với tập kiểm tra có thể gây rắc rối nếu không có một kế hoạch nhất quán để thích nghi với tập xác định mới.
```
**Ban Y:**
```
Tập huấn luyện gồm các bức ảnh, trong khi tập kiểm tra chỉ chứa tranh biếm họa. Huấn luyện trên một bộ dữ liệu có đặc tính khác biệt đáng kể so với tập kiểm tra có thể gây rắc rối nếu không có một kế hoạch nhất quán để thích nghi với miền mới.
```
- Overall: Y
- Style: HÒA

---

## Cau 31

**EN:**
```
The model below applies dropout to the output
of each hidden layer (following the activation function).
We can set dropout probabilities for each layer separately.
A common trend is to set
a lower dropout probability closer to the input layer.
Below we set it to 0.2 and 0.5 for the first
and second hidden layers, respectively.
We ensure that dropout is only active during training.
```
**Ban X:**
```
Mô hình dưới đây áp dụng dropout lên đầu ra của mỗi tầng ẩn (sau hàm kích hoạt). Chúng ta có thể đặt xác suất dropout riêng cho từng tầng. Một xu hướng phổ biến là đặt xác suất dropout thấp hơn ở gần tầng đầu vào hơn. Dưới đây, chúng ta đặt lần lượt là 0.2 và 0.5 cho tầng ẩn thứ nhất và thứ hai. Chúng ta đảm bảo rằng dropout chỉ hoạt động trong quá trình training.
```
**Ban Y:**
```
Mô hình dưới đây áp dụng dropout lên đầu ra của mỗi lớp ẩn (sau hàm kích hoạt). Chúng ta có thể đặt xác suất dropout riêng cho từng lớp. Một xu hướng phổ biến là đặt xác suất dropout thấp hơn ở gần lớp đầu vào hơn. Bên dưới, chúng ta đặt lần lượt là 0.2 và 0.5 cho lớp ẩn thứ nhất và thứ hai. Chúng ta đảm bảo rằng dropout chỉ hoạt động trong quá trình huấn luyện.
```
- Overall: Y
- Style: Y

---

## Cau 32

**EN:**
```
When we train our models, we attempt to search for a function
that fits the training data as well as possible.
If the function is so flexible that it can catch on to spurious patterns
just as easily as to true associations,
then it might perform *too well* without producing a model
that generalizes well to unseen data.
This is precisely what we want to avoid or at least control.
Many of the techniques in deep learning are heuristics and tricks
aimed at guarding against overfitting.
```
**Ban X:**
```
Khi huấn luyện các mô hình, chúng ta cố gắng tìm kiếm một hàm khớp với dữ liệu huấn luyện tốt nhất có thể. Nếu hàm đó đủ linh hoạt để bám vào các mẫu nhiễu cũng dễ dàng như các mối liên hệ thật, thì nó có thể hoạt động *quá tốt* mà không tạo ra một mô hình khái quát hóa tốt trên dữ liệu chưa thấy. Đây chính là điều chúng ta muốn tránh hoặc ít nhất là kiểm soát. Nhiều kỹ thuật trong học sâu là các kinh nghiệm và mẹo nhằm bảo vệ khỏi hiện tượng quá khớp.
```
**Ban Y:**
```
Khi chúng ta huấn luyện các mô hình, chúng ta cố gắng tìm kiếm một hàm khớp với dữ liệu huấn luyện tốt nhất có thể. Nếu hàm đó linh hoạt đến mức có thể nắm bắt các mẫu nhiễu cũng dễ dàng như các mối liên hệ thực sự, thì nó có thể hoạt động *quá tốt* mà không tạo ra một mô hình khái quát hóa tốt trên dữ liệu chưa thấy. Đây chính xác là điều chúng ta muốn tránh hoặc ít nhất là kiểm soát. Nhiều kỹ thuật trong học sâu là các kinh nghiệm và mẹo nhằm bảo vệ chống lại quá khớp.
```
- Overall: X
- Style: X

---

## Cau 33

**EN:**
```
Then, in 2014, Srivastava et al. :cite:`Srivastava.Hinton.Krizhevsky.ea.2014`
developed a clever idea for how to apply Bishop's idea
to the internal layers of a network, too.
Namely, they proposed to inject noise
into each layer of the network
before calculating the subsequent layer during training.
They realized that when training
a deep network with many layers,
injecting noise enforces smoothness just on the input-output mapping.
```
**Ban X:**
```
Sau đó, vào năm 2014, Srivastava và cộng sự :cite:`Srivastava.Hinton.Krizhevsky.ea.2014` đã phát triển một ý tưởng thông minh về cách áp dụng ý tưởng của Bishop cho các lớp bên trong của một mạng nữa. Cụ thể, họ đề xuất đưa nhiễu vào từng tầng của mạng trước khi tính tầng tiếp theo trong quá trình training. Họ nhận ra rằng khi training một mạng sâu với nhiều tầng, việc đưa nhiễu vào chỉ ràng buộc tính trơn của ánh xạ vào-ra.
```
**Ban Y:**
```
Sau đó, vào năm 2014, Srivastava và cộng sự :cite:`Srivastava.Hinton.Krizhevsky.ea.2014` đã phát triển một ý tưởng khéo léo về cách áp dụng ý tưởng của Bishop cho cả các tầng bên trong của mạng. Cụ thể, họ đề xuất đưa nhiễu vào từng tầng của mạng trước khi tính tầng kế tiếp trong quá trình huấn luyện. Họ nhận ra rằng khi huấn luyện một mạng sâu với nhiều tầng, việc đưa nhiễu vào sẽ chỉ buộc tính trơn đối với ánh xạ vào-ra.
```
- Overall: Y
- Style: Y

---

## Cau 34

**EN:**
```
With MLPs, for example,
we may wish to compare models with
different numbers of hidden layers,
different numbers of hidden units,
and various choices of the activation functions
applied to each hidden layer.
In order to determine the best among our candidate models,
we will typically employ a validation dataset.
```
**Ban X:**
```
Với MLP, chẳng hạn, chúng ta có thể muốn so sánh các mô hình với số lượng tầng ẩn khác nhau, số lượng đơn vị ẩn khác nhau, và các lựa chọn khác nhau của các hàm kích hoạt được áp dụng cho mỗi tầng ẩn. Để xác định mô hình tốt nhất trong số các mô hình ứng viên, chúng ta thường sẽ sử dụng một tập xác thực.
```
**Ban Y:**
```
Với MLP, chẳng hạn, ta có thể muốn so sánh các mô hình với số lượng tầng ẩn khác nhau, số lượng đơn vị ẩn khác nhau, và các lựa chọn khác nhau của các hàm kích hoạt được áp dụng cho từng tầng ẩn. Để xác định mô hình tốt nhất trong số các mô hình ứng viên, ta thường sử dụng một tập dữ liệu xác thực.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 35

**EN:**
```
When we have simple models and abundant data,
we expect the generalization error to resemble the training error.
When we work with more complex models and fewer examples,
we expect the training error to go down but the generalization gap to grow.
What precisely constitutes model complexity is a complex matter.
Many factors govern whether a model will generalize well.
For example a model with more parameters might be considered more complex.
A model whose parameters can take a wider range of values
might be more complex.
Often with neural networks, we think of a model
that takes more training iterations as more complex,
and one subject to *early stopping* (fewer training iterations) as less complex.
```
**Ban X:**
```
Khi chúng ta có các mô hình đơn giản và dữ liệu dồi dào, ta kỳ vọng lỗi khái quát hóa sẽ giống với lỗi huấn luyện. Khi làm việc với các mô hình phức tạp hơn và ít mẫu hơn, ta kỳ vọng lỗi huấn luyện sẽ giảm nhưng khoảng cách khái quát hóa sẽ tăng. Chính xác điều gì cấu thành độ phức tạp mô hình là một vấn đề phức tạp. Nhiều yếu tố chi phối việc một mô hình có khái quát hóa tốt hay không. Ví dụ, một mô hình có nhiều tham số hơn có thể được xem là phức tạp hơn. Một mô hình mà các tham số của nó có thể nhận một phạm vi giá trị rộng hơn cũng có thể phức tạp hơn. Thường thì với mạng nơ-ron, ta xem một mô hình cần nhiều vòng lặp training hơn là phức tạp hơn, và một mô hình chịu *dừng sớm* (ít vòng lặp training hơn) là ít phức tạp hơn.
```
**Ban Y:**
```
Khi chúng ta có các mô hình đơn giản và dữ liệu dồi dào, ta kỳ vọng sai số khái quát hóa sẽ giống với sai số huấn luyện. Khi làm việc với các mô hình phức tạp hơn và ít ví dụ hơn, ta kỳ vọng sai số huấn luyện sẽ giảm nhưng khoảng cách khái quát hóa sẽ tăng. Điều gì chính xác cấu thành độ phức tạp của mô hình là một vấn đề phức tạp. Có nhiều yếu tố chi phối việc một mô hình có khái quát hóa tốt hay không. Ví dụ, một mô hình có nhiều tham số hơn có thể được xem là phức tạp hơn. Một mô hình mà các tham số của nó có thể nhận một phạm vi giá trị rộng hơn có thể phức tạp hơn. Thường thì với mạng nơ-ron, ta xem một mô hình cần nhiều vòng lặp huấn luyện hơn là phức tạp hơn, và một mô hình chịu *dừng sớm* (ít vòng lặp huấn luyện hơn) là ít phức tạp hơn.
```
- Overall: Y
- Style: Y

---

## Cau 36

**EN:**
```
A similar thing happened to the US Army
when they first tried to detect tanks in the forest.
They took aerial photographs of the forest without tanks,
then drove the tanks into the forest
and took another set of pictures.
The classifier appeared to work *perfectly*.
Unfortunately, it had merely learned
how to distinguish trees with shadows
from trees without shadows---the first set
of pictures was taken in the early morning,
the second set at noon.
```
**Ban X:**
```
Một điều tương tự đã xảy ra với Quân đội Hoa Kỳ khi họ lần đầu cố gắng phát hiện xe tăng trong rừng. Họ chụp ảnh trên không khu rừng không có xe tăng, rồi cho xe tăng chạy vào rừng và chụp một bộ ảnh khác. Bộ phân loại dường như hoạt động *hoàn hảo*. Đáng tiếc, nó chỉ đơn thuần học cách phân biệt cây có bóng với cây không có bóng---bộ ảnh đầu tiên được chụp vào sáng sớm, bộ ảnh thứ hai vào buổi trưa.
```
**Ban Y:**
```
Một điều tương tự đã xảy ra với Quân đội Hoa Kỳ khi họ lần đầu cố gắng phát hiện xe tăng trong rừng. Họ chụp ảnh trên không khu rừng không có xe tăng, rồi lái xe tăng vào rừng và chụp một bộ ảnh khác. Bộ phân loại dường như hoạt động *hoàn hảo*. Đáng tiếc, nó chỉ đơn thuần học cách phân biệt cây có bóng với cây không có bóng---bộ ảnh thứ nhất được chụp vào sáng sớm, bộ ảnh thứ hai vào buổi trưa.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 37

**EN:**
```
When training neural networks,
forward and backward propagation depend on each other.
In particular, for forward propagation,
we traverse the computational graph in the direction of dependencies
and compute all the variables on its path.
These are then used for backpropagation
where the compute order on the graph is reversed.
```
**Ban X:**
```
Khi huấn luyện mạng nơ-ron, lan truyền xuôi và lan truyền ngược phụ thuộc lẫn nhau. Cụ thể, với lan truyền xuôi, chúng ta duyệt đồ thị tính toán theo chiều của các phụ thuộc và tính tất cả các biến trên đường đi của nó. Sau đó, chúng được dùng cho lan truyền ngược, trong đó thứ tự tính toán trên đồ thị được đảo ngược.
```
**Ban Y:**
```
Khi huấn luyện mạng nơ-ron, lan truyền xuôi và lan truyền ngược phụ thuộc lẫn nhau. Cụ thể, trong lan truyền xuôi, chúng ta duyệt đồ thị tính toán theo chiều của các phụ thuộc và tính tất cả các biến trên đường đi của nó. Các biến này sau đó được dùng cho lan truyền ngược, khi đó thứ tự tính toán trên đồ thị được đảo ngược.
```
- Overall: HÒA
- Style: Y

---

## Cau 38

**EN:**
```
We can see that in each example, (**the first feature is the ID.**)
This helps the model identify each training example.
While this is convenient, it does not carry
any information for prediction purposes.
Hence, (**we remove it from the dataset**)
before feeding the data into the model.
```
**Ban X:**
```
Ta có thể thấy rằng trong mỗi ví dụ, (**đặc trưng đầu tiên là ID.**) Điều này giúp mô hình nhận diện từng ví dụ huấn luyện. Tuy tiện lợi, nó không mang bất kỳ thông tin nào phục vụ cho mục đích dự đoán. Vì vậy, (**chúng ta loại bỏ nó khỏi tập dữ liệu**) trước khi đưa dữ liệu vào mô hình.
```
**Ban Y:**
```
Ta có thể thấy rằng trong mỗi mẫu, (**đặc trưng đầu tiên là ID.**) Điều này giúp mô hình nhận diện từng mẫu huấn luyện. Mặc dù điều này tiện lợi, nó không mang theo bất kỳ thông tin nào cho mục đích dự đoán. Vì thế, (**chúng ta loại bỏ nó khỏi bộ dữ liệu**) trước khi đưa dữ liệu vào mô hình.
```
- Overall: HÒA
- Style: HÒA

---

## Cau 39

**EN:**
```
## Overfitting Revisited
```
**Ban X:**
```
## Xem xét lại hiện tượng quá khớp
```
**Ban Y:**
```
## Xem xét lại quá khớp
```
- Overall: X
- Style: X

---

## Cau 40

**EN:**
```
Thus, we should never rely on the test data for model selection.
And yet we cannot rely solely on the training data
for model selection either because
we cannot estimate the generalization error
on the very data that we use to train the model.
```
**Ban X:**
```
Vì vậy, chúng ta tuyệt đối không nên dựa vào dữ liệu kiểm tra để chọn mô hình. Tuy nhiên, chúng ta cũng không thể chỉ dựa vào dữ liệu huấn luyện để chọn mô hình, bởi vì ta không thể ước lượng sai số khái quát hóa trên chính dữ liệu mà ta dùng để huấn luyện mô hình.
```
**Ban Y:**
```
Vì vậy, chúng ta không bao giờ nên dựa vào dữ liệu kiểm tra để chọn mô hình. Và cũng không thể chỉ dựa vào dữ liệu huấn luyện để chọn mô hình, vì chúng ta không thể ước lượng lỗi khái quát hóa trên chính dữ liệu mà chúng ta dùng để huấn luyện mô hình.
```
- Overall: HÒA
- Style: X

---
