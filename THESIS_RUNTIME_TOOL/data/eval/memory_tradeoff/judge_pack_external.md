# BLIND JUDGE PACK — technical ML/DL terminology (English->Vietnamese)

You are evaluating a technical Deep Learning / Machine Learning textbook translated EN->VI.
For each item you get the English source sentence, ONE English term, and TWO candidate Vietnamese
renderings (Version A, Version B) shown in context with the term marked by guillemets «...».
Decide which rendering of the MARKED term «...» better conveys the English term IN THIS CONTEXT.

Rules:
- Judge ONLY the marked «term». Ignore other wording differences.
- Weigh: (1) semantic accuracy vs the English term in this ML/DL context, (2) naturalness in
  Vietnamese technical writing, (3) correct register (established ML term vs everyday word).
- You do NOT know which system produced A or B. Do not guess.
- If both are equally acceptable, answer "equivalent". Do NOT force a winner.

OUTPUT: a single JSON array, one object per item, NOTHING else:
[{"item_id":"...","label":"A_better|B_better|equivalent","reason":"<short>"}, ...]

## ITEMS (57)

### 1. item_id: mt_fashion_mnist_dataset_b8ddab06eb
English sentence: We will use the similar, but more complex Fashion-MNIST dataset~~)
English term: Fashion-MNIST dataset
Version A: (Chúng ta sẽ sử dụng «bộ dữ liệu Fashion-MNIST» tương tự nhưng phức tạp hơn)
Version B: Chúng ta sẽ dùng «tập dữ liệu Fashion-MNIST» tương tự nhưng phức tạp hơn~~)

### 2. item_id: mt_mnist_dataset_923cfe4011
English sentence: The MNIST dataset with its 60000 handwritten digits was considered huge.
English term: MNIST dataset
Version A: Bộ dữ liệu «MNIST» với 60000 chữ số viết tay được xem là rất lớn.
Version B: «Bộ dữ liệu MNIST» với 60000 chữ số viết tay được xem là rất lớn.

### 3. item_id: mt_universal_approximators_c7cc5b991a
English sentence: ### Universal Approximators
English term: Universal Approximators
Version A: ### «Bộ xấp xỉ phổ quát»
Version B: ### «Bộ xấp xỉ phổ dụng»

### 4. item_id: mt_vanishing_and_exploding_gradients_c51a803991
English sentence: * Vanishing and exploding gradients are common issues in deep networks.
English term: Vanishing and Exploding Gradients
Version A: * «Độ dốc biến mất và bùng nổ» là các vấn đề phổ biến trong mạng sâu.
Version B: * «Gradient biến mất và bùng nổ» là những vấn đề phổ biến trong các mạng sâu.

### 5. item_id: mt_activations_9afd89eb88
English sentence: are called *activations*.
English term: activations
Version A: Đầu ra của các hàm kích hoạt (ví dụ, $\sigma(\cdot)$) được gọi là *«giá trị kích hoạt»*.
Version B: Đầu ra của các hàm kích hoạt (ví dụ, $\sigma(\cdot)$) được gọi là *«các kích hoạt»*.

### 6. item_id: mt_additive_noise_7a444cd8ee
English sentence: Assume that the noise model governing the additive noise $\epsilon$ is the exponential distribution.
English term: additive noise
Version A: Giả sử mô hình nhiễu chi phối «nhiễu cộng» $\epsilon$ là phân phối mũ.
Version B: Giả sử mô hình nhiễu chi phối «nhiễu cộng thêm» $\epsilon$ là phân phối mũ.

### 7. item_id: mt_affine_transformations_e8551c005d
English sentence: For quadratic losses and affine transformations,
English term: affine transformations
Version A: Với các hàm mất mát bậc hai và các «biến đổi affine», chúng ta có thể viết rõ ra như sau:
Version B: Với các loss bậc hai và các «phép biến đổi affine», ta có thể viết rõ như sau:

### 8. item_id: mt_annotation_2e76f54422
English sentence: and grammatical assumptions to get some annotation.
English term: annotation
Version A: Việc này liên quan đến «chú giải» một chuỗi văn bản bằng các thuộc tính.
Version B: **«Gán nhãn» và phân tích cú pháp**.

### 9. item_id: mt_bandits_5d196d3aa2
English sentence: ### Bandits
English term: bandits
Version A: ### «Bài toán băng-đit»
Version B: ### «Bandit»

### 10. item_id: mt_batch_size_115bac8f95
English sentence: If the number of examples cannot be divided by the batch size, what happens to the `data_iter` function's behavior?
English term: batch size
Version A: Nếu số lượng ví dụ không thể chia hết cho «kích thước minibatch», điều gì sẽ xảy ra với hành vi của hàm `data_iter`?
Version B: Nếu số lượng mẫu không thể chia hết cho «kích thước lô», điều gì sẽ xảy ra với hành vi của hàm `data_iter`?

### 11. item_id: mt_covariate_shift_a77c8a8dfb
English sentence: ### Covariate Shift
English term: covariate shift
Version A: ### «Dịch chuyển hiệp biến»
Version B: ### «Lệch hiệp biến»

### 12. item_id: mt_data_examples_7c5e6ddeae
English sentence: If these assumptions held true, then given these two data examples,
English term: data examples
Version A: Nếu những giả định này là đúng, thì với hai «mẫu dữ liệu» này, bạn đã có thể xác định cấu trúc định giá của người thợ: 100 đô la mỗi giờ cộng với 50 đô la để đến nhà bạn.
Version B: Nếu những giả định này là đúng, thì với hai «ví dụ dữ liệu» này, bạn đã có thể xác định cấu trúc định giá của nhà thầu: 100 đô la mỗi giờ cộng với 50 đô la để đến nhà bạn.

### 13. item_id: mt_data_instance_cacbc74d36
English sentence: Each *example* (or *data point*, *data instance*, *sample*) typically consists of a set
English term: data instance
Version A: Mỗi *mẫu* (hoặc *điểm dữ liệu*, *«thể hiện dữ liệu»*, *mẫu*) thường bao gồm một tập hợp các thuộc tính gọi là *đặc trưng* (hoặc *biến đồng biến*), từ đó mô hình phải đưa ra dự đoán của mình.
Version B: Mỗi *ví dụ* (hoặc *điểm dữ liệu*, *«thực thể dữ liệu»*, *mẫu*) thường gồm một tập các thuộc tính gọi là *đặc trưng* (hoặc *biến đồng biến*), từ đó mô hình phải đưa ra dự đoán của mình.

### 14. item_id: mt_deferred_0db493d58e
English sentence: the initialization is actually *deferred*.
English term: deferred
Version A: Gluon cho phép điều này vì ở phía sau, việc khởi tạo thực ra được *«hoãn lại»*.
Version B: Gluon cho phép chúng ta làm như vậy vì ở phía sau, việc khởi tạo thực ra là *«trì hoãn»*.

### 15. item_id: mt_deletion_013e5670c0
English sentence: To handle missing data, typical methods include *imputation* and *deletion*,
English term: deletion
Version A: Để xử lý dữ liệu thiếu, các phương pháp điển hình bao gồm *điền khuyết* và *«loại bỏ»*, trong đó điền khuyết thay thế các giá trị thiếu bằng các giá trị được thay thế, còn loại bỏ thì bỏ qua các giá trị thiếu.
Version B: Để xử lý dữ liệu thiếu, các phương pháp điển hình bao gồm *điền khuyết* và *«xóa bỏ»*, trong đó điền khuyết thay thế giá trị thiếu bằng các giá trị thay thế, còn xóa bỏ thì bỏ qua giá trị thiếu.

### 16. item_id: mt_efficiency_fff33dfb39
English sentence: To improve computational efficiency and take advantage of GPUs,
English term: efficiency
Version A: Để cải thiện «hiệu quả» tính toán và tận dụng GPU, chúng ta thường thực hiện các phép tính vector cho các minibatch dữ liệu.
Version B: Để cải thiện «tính hiệu quả» tính toán và tận dụng GPU, chúng ta thường thực hiện các phép tính vector cho các minibatch dữ liệu.

### 17. item_id: mt_elementwise_1d6feafbec
English sentence: are the *elementwise* operations.
English term: elementwise
Version A: Một số phép toán đơn giản và hữu ích nhất là các phép toán *theo «từng phần tử»*.
Version B: Một số phép toán đơn giản nhất và hữu ích nhất là các phép toán *«theo phần tử»*.

### 18. item_id: mt_elementwise_multiplication_0ebdbc19e3
English sentence: [**elementwise multiplication of two matrices is called their *Hadamard product***]
English term: elementwise multiplication
Version A: Cụ thể, [**«phép nhân theo phần tử» của hai ma trận được gọi là *tích Hadamard***] (ký hiệu toán học $\odot$).
Version B: Cụ thể, [**phép «nhân theo từng phần tử» của hai ma trận được gọi là *tích Hadamard***] (ký hiệu toán học $ odot$).

### 19. item_id: mt_end_to_end_training_e0ecc93532
English sentence: Arguably the most significant commonality in deep learning methods is the use of *end-to-end training*.
English term: end-to-end training
Version A: Có lẽ điểm chung quan trọng nhất trong các phương pháp học sâu là việc sử dụng *«huấn luyện đầu cuối»*.
Version B: Có lẽ điểm chung quan trọng nhất trong các phương pháp học sâu là việc sử dụng *«huấn luyện đầu-cuối»*.

### 20. item_id: mt_feature_engineering_fbcf9d9a88
English sentence: For instance, in computer vision scientists used to separate the process of *feature engineering* from the process of building machine learning models.
English term: feature engineering
Version A: Chẳng hạn, trong thị giác máy tính, các nhà khoa học trước đây thường tách quá trình *«kỹ thuật đặc trưng»* khỏi quá trình xây dựng mô hình học máy.
Version B: Chẳng hạn, trong thị giác máy tính, trước đây các nhà khoa học thường tách quá trình *«thiết kế đặc trưng»* khỏi quá trình xây dựng các mô hình học máy.

### 21. item_id: mt_feature_selection_ec5e3cb404
English sentence: This is called *feature selection*,
English term: feature selection
Version A: Điều này được gọi là *«lựa chọn đặc trưng»*, và có thể hữu ích vì những lý do khác.
Version B: Điều này được gọi là *«chọn đặc trưng»*, vốn có thể mong muốn vì những lý do khác.

### 22. item_id: mt_feature_vectors_9cfc7e31ab
English sentence: 2004` reigned supreme for over a decade as algorithms for mapping images into feature vectors.
English term: feature vectors
Version A: 2004` đã thống trị trong hơn một thập kỷ như các thuật toán ánh xạ ảnh thành các «vector đặc trưng».
Version B: 2004` đã thống trị trong hơn một thập kỷ như các thuật toán để ánh xạ ảnh thành các «vectơ đặc trưng».

### 23. item_id: mt_feedback_loops_73b8202c83
English sentence: Thus it is possible for feedback loops to form
English term: feedback loops
Version A: Vì vậy, có thể hình thành các «vòng phản hồi», trong đó một hệ thống gợi ý ưu tiên đẩy một mục lên, rồi mục đó lại được xem là tốt hơn (do được mua nhiều hơn) và đến lượt nó lại được gợi ý thường xuyên hơn.
Version B: Vì vậy, có thể hình thành các «vòng lặp phản hồi», trong đó một hệ thống gợi ý ưu tiên đẩy một mục lên, rồi mục đó lại được xem là tốt hơn (do được mua nhiều hơn), và đến lượt nó lại được gợi ý thường xuyên hơn.

### 24. item_id: mt_framework_f178867da2
English sentence: No matter which framework you use,
English term: framework
Version A: Dù bạn dùng «khung phần mềm» nào, *lớp tensor* của nó (`ndarray` trong MXNet, `Tensor` trong cả PyTorch và TensorFlow) đều tương tự `ndarray` của NumPy với một vài tính năng nổi bật.
Version B: Dù bạn dùng «framework» nào, *lớp tensor* của nó (`ndarray` trong MXNet, `Tensor` trong cả PyTorch và TensorFlow) đều tương tự `ndarray` của NumPy với một vài tính năng nổi bật.

### 25. item_id: mt_generalization_gap_29c7c2502f
English sentence: we expect the training error to go down but the generalization gap to grow.
English term: generalization gap
Version A: Khi làm việc với các mô hình phức tạp hơn và ít ví dụ hơn, ta kỳ vọng sai số huấn luyện sẽ giảm nhưng «khoảng cách khái quát hóa» sẽ tăng.
Version B: Khi làm việc với các mô hình phức tạp hơn và ít mẫu hơn, ta kỳ vọng lỗi huấn luyện sẽ giảm nhưng «khoảng cách tổng quát hóa» sẽ tăng.

### 26. item_id: mt_heuristics_a628d3bfd9
English sentence: Many of the techniques in deep learning are heuristics and tricks
English term: heuristics
Version A: Nhiều kỹ thuật trong học sâu là các «phương pháp heuristic» và mẹo nhằm bảo vệ chống lại quá khớp.
Version B: Nhiều kỹ thuật trong học sâu là các «kinh nghiệm» và mẹo nhằm bảo vệ khỏi hiện tượng quá khớp.

### 27. item_id: mt_learnable_parameters_11a624f504
English sentence: increasing the number of learnable parameters.
English term: learnable parameters
Version A: * Các phương pháp mới để điều khiển năng lực, chẳng hạn như *dropout*   :cite:`Srivastava.Hinton.Krizhevsky.ea.2014`,   đã giúp giảm thiểu nguy cơ quá khớp.   Điều này đạt được bằng cách áp dụng tiêm nhiễu :cite:`Bishop.1995`   xuyên suốt mạng nơ-ron, thay thế trọng số bằng các biến ngẫu nhiên   cho mục đích huấn luyện. * Các cơ chế chú ý đã giải quyết một vấn đề thứ hai   đã làm khổ thống kê trong hơn một thế kỷ:   làm thế nào để tăng bộ nhớ và độ phức tạp của một hệ thống mà không   làm tăng số lượng «tham số học được».   Các nhà nghiên cứu đã tìm ra một giải pháp thanh lịch   bằng cách sử dụng cái chỉ có thể được xem như một cấu trúc con trỏ có thể học được :cite:`Bahdanau.Cho.Bengio.2014`.   Thay vì phải ghi nhớ toàn bộ một chuỗi văn bản, ví dụ,   cho dịch máy trong một biểu diễn có số chiều cố định,   tất cả những gì cần lưu trữ chỉ là một con trỏ tới trạng thái trung gian   của quá trình dịch. Điều này cho phép tăng đáng kể độ chính xác   đối với các chuỗi dài, vì mô hình   không còn cần phải ghi nhớ toàn bộ chuỗi trước khi   bắt đầu tạo ra một chuỗi mới. * Các thiết kế nhiều giai đoạn, ví dụ, thông qua memory networks    :cite:`Sukhbaatar.Weston.Fergus.ea.2015` và neural programmer-interpreter :cite:`Reed.De-Freitas.2015`   đã cho phép các nhà mô hình hóa thống kê mô tả các cách tiếp cận suy luận lặp đi lặp lại. Các công cụ này cho phép trạng thái bên trong của mạng nơ-ron sâu   được sửa đổi nhiều lần, qua đó thực hiện các bước tiếp theo   trong một chuỗi suy luận, tương tự như cách một bộ xử lý   có thể sửa đổi bộ nhớ cho một tính toán. * Một phát triển then chốt khác là sự ra đời của mạng đối nghịch sinh   :cite:`Goodfellow.Pouget-Abadie.Mirza.ea.2014`.   Theo truyền thống, các phương pháp thống kê cho ước lượng mật độ   và mô hình sinh tập trung vào việc tìm các phân phối xác suất phù hợp   và các thuật toán (thường là xấp xỉ) để lấy mẫu từ chúng.   Kết quả là, các thuật toán này phần lớn bị giới hạn bởi sự thiếu   linh hoạt vốn có trong các mô hình thống kê.   Đổi mới then chốt trong mạng đối nghịch sinh là thay bộ lấy mẫu   bằng một thuật toán tùy ý với các tham số khả vi.   Sau đó, các tham số này được điều chỉnh sao cho bộ phân biệt   (về thực chất là một kiểm định hai mẫu) không thể phân biệt dữ liệu giả với dữ liệu thật.   Thông qua khả năng sử dụng các thuật toán tùy ý để tạo dữ liệu,   nó đã mở rộng ước lượng mật độ sang một loạt kỹ thuật rất đa dạng.   Các ví dụ về ngựa vằn phi nước đại :cite:`Zhu.Park.Isola.ea.2017`   và về khuôn mặt người nổi tiếng giả :cite:`Karras.Aila.Laine.ea.2017`   đều là minh chứng cho tiến bộ này.   Ngay cả những người vẽ nguệch ngoạc nghiệp dư cũng có thể tạo ra   ảnh chân thực dựa chỉ trên các bản phác thảo mô tả   bố cục của một cảnh trông như thế nào :cite:`Park.Liu.Wang.ea.2019`. * Trong nhiều trường hợp, một GPU đơn lẻ là không đủ để xử lý   lượng dữ liệu lớn sẵn có cho huấn luyện.   Trong thập kỷ qua, khả năng xây dựng các thuật toán huấn luyện song song và   phân tán đã được cải thiện đáng kể.   Một trong những thách thức chính khi thiết kế các thuật toán có khả năng mở rộng   là công cụ chủ lực của tối ưu hóa học sâu,   hạ gradient ngẫu nhiên, phụ thuộc vào các mini lô dữ liệu tương đối   nhỏ để được xử lý.   Đồng thời, các lô nhỏ làm hạn chế hiệu quả của GPU.   Vì vậy, huấn luyện trên 1024 GPU với kích thước minibatch,   chẳng hạn 32 ảnh mỗi lô, tương đương với một minibatch tổng hợp   khoảng 32000 ảnh. Các công trình gần đây, đầu tiên bởi Li :cite:`Li.2017`,   và sau đó bởi :cite:`You.Gitman.Ginsburg.2017`   và :cite:`Jia.Song.He.ea.2018` đã nâng kích thước lên 64000 quan sát,   giảm thời gian huấn luyện cho mô hình ResNet-50 trên tập dữ liệu ImageNet xuống dưới 7 phút.   Để so sánh---ban đầu thời gian huấn luyện được đo theo đơn vị ngày. * Khả năng song song hóa tính toán cũng đã đóng góp cực kỳ quan trọng   vào tiến bộ trong học tăng cường, ít nhất là bất cứ khi nào mô phỏng là một   lựa chọn. Điều này đã dẫn đến tiến bộ đáng kể trong việc máy tính đạt được   hiệu năng vượt con người trong Go, các trò chơi Atari, Starcraft, và trong các   mô phỏng vật lý (ví dụ, sử dụng MuJoCo). Xem ví dụ,   :cite:`Silver.Huang.Maddison.ea.2016` để biết mô tả   về cách đạt được điều này trong AlphaGo. Nói ngắn gọn,   học tăng cường hoạt động tốt nhất nếu có sẵn nhiều bộ ba (trạng thái, hành động, phần thưởng), tức là, bất cứ khi nào có thể thử nhiều thứ để học cách chúng liên hệ với nhau. Mô phỏng cung cấp một con đường như vậy. * Các khung học sâu đã đóng vai trò then chốt   trong việc phổ biến các ý tưởng. Thế hệ đầu tiên của các khung cho phép mô hình hóa dễ dàng bao gồm   [Caffe](https://github.com/BVLC/caffe),   [Torch](https://github.com/torch), và   [Theano](https://github.com/Theano/Theano).   Nhiều bài báo nền tảng đã được viết bằng các công cụ này.   Đến nay, chúng đã được thay thế bởi   [TensorFlow](https://github.com/tensorflow/tensorflow) (thường được dùng thông qua API cấp cao [Keras](https://github.com/keras-team/keras)), [CNTK](https://github.com/Microsoft/CNTK), [Caffe 2](https://github.com/caffe2/caffe2), và [Apache MXNet](https://github.com/apache/incubator-mxnet). Thế hệ công cụ thứ ba, cụ thể là các công cụ mệnh lệnh cho học sâu,   có thể nói đã được tiên phong bởi [Chainer](https://github.com/chainer/chainer),   vốn dùng cú pháp tương tự Python NumPy để mô tả các mô hình.   Ý tưởng này đã được cả [PyTorch](https://github.com/pytorch/pytorch),   [Gluon API](https://github.com/apache/incubator-mxnet) của MXNet, và [Jax](https://github.com/google/jax) tiếp nhận.
Version B: * Cơ chế chú ý đã giải quyết một vấn đề thứ hai vốn đã làm khổ ngành thống kê trong hơn một thế kỷ: làm thế nào để tăng bộ nhớ và độ phức tạp của một hệ thống mà không làm tăng số lượng «tham số có thể học».

### 28. item_id: mt_lifting_8f516a1fc1
English sentence: by *lifting* the scalar function to an elementwise vector operation.
English term: lifting
Version A: Ở đây, chúng ta đã tạo ra hàm có giá trị vector $F: \mathbb{R}^d, \mathbb{R}^d \rightarrow \mathbb{R}^d$ bằng cách *«nâng»* hàm vô hướng thành một phép toán vector theo từng phần tử.
Version B: Ở đây, chúng ta đã tạo ra hàm $F: \mathbb{R}^d, \mathbb{R}^d \rightarrow \mathbb{R}^d$ dạng vectơ bằng cách *«nâng ánh xạ»* hàm vô hướng thành một phép toán vector theo phần tử.

### 29. item_id: mt_manipulating_8325f6c2ab
English sentence: for storing, manipulating, and preprocessing data.
English term: manipulating
Version A: Vì vậy, chúng ta sẽ bắt đầu bằng cách học các kỹ năng thực hành để lưu trữ, «xử lý» và tiền xử lý dữ liệu.
Version B: Vì vậy, chúng ta sẽ bắt đầu bằng cách học các kỹ năng thực hành để lưu trữ, «thao tác» và tiền xử lý dữ liệu.

### 30. item_id: mt_membership_99ef7e9bc5
English sentence: and simply denotes membership in a set.
English term: membership
Version A: Ký hiệu $\in$ có thể được đọc là “thuộc” và đơn giản biểu thị «quan hệ thuộc» trong một tập hợp.
Version B: Ký hiệu $\in$ có thể được đọc là “«thuộc»” và đơn giản biểu thị sự thuộc về một tập hợp.

### 31. item_id: mt_model_complexity_ca64fe25a6
English sentence: ### Model Complexity
English term: model complexity
Version A: ### «Độ phức tạp mô hình»
Version B: ### «Độ phức tạp của mô hình»

### 32. item_id: mt_model_s_parameters_eb636b643b
English sentence: An *algorithm* to adjust the model's parameters to optimize the objective function.
English term: model's parameters
Version A: Một *thuật toán* để điều chỉnh các «tham số mô hình» nhằm tối ưu hóa hàm mục tiêu.
Version B: Một *thuật toán* để điều chỉnh các «tham số của mô hình» nhằm tối ưu hóa hàm mục tiêu.

### 33. item_id: mt_multivariate_functions_de4f1ded26
English sentence: Let $\mathbf{x}$ be an $n$-dimensional vector, the following rules are often used when differentiating multivariate functions:
English term: multivariate functions
Version A: abla{\mathbf{x}}$ là một vectơ $n$ chiều, các quy tắc sau thường được dùng khi lấy đạo hàm các «hàm nhiều biến»:
Version B: abla$ là một vector $n$ chiều, các luật sau thường được dùng khi vi phân «hàm đa biến»:

### 34. item_id: mt_negative_log_likelihood_006bbe2ce2
English sentence: Write out the negative log-likelihood of the data under the model $-\log P(\mathbf y \mid \mathbf X)$.
English term: negative log-likelihood
Version A: Viết ra «âm log-khả năng» của dữ liệu dưới mô hình $-\log P(\mathbf y \mid \mathbf X)$.
Version B: Viết «log-khả năng âm» của dữ liệu dưới mô hình $-\log P(\mathbf y \mid \mathbf X)$.

### 35. item_id: mt_offset_53a610e925
English sentence: (also called an *offset* or *intercept*).
English term: offset
Version A: Trong :eqref:`eq_price-area`, $w_{\mathrm{area}}$ và $w_{\mathrm{age}}$ được gọi là *trọng số*, và $b$ được gọi là *«độ lệch»* (còn gọi là *offset* hoặc *intercept*).
Version B: Trong :eqref:`eq_price-area`, $w_{\mathrm{area}}$ và $w_{\mathrm{age}}$ được gọi là *trọng số*, và $b$ được gọi là *độ lệch* (còn gọi là *«độ dời»* hoặc *intercept*).

### 36. item_id: mt_prior_4a47653d5f
English sentence: In Bayesian statistics we use the product of prior and likelihood to arrive at a posterior via $P(w \mid x) \propto P(x \mid w) P(w)$.
English term: prior
Version A: Trong thống kê Bayes, chúng ta dùng tích của «tiên nghiệm» và hàm hợp lý để đi đến hậu nghiệm thông qua $P(w \mid x) \propto P(x \mid w) P(w)$.
Version B: Trong thống kê Bayes, chúng ta dùng tích của «phân phối tiên nghiệm» và hợp lý để đi đến phân phối hậu nghiệm thông qua $P(w \mid x) \propto P(x \mid w) P(w)$.

### 37. item_id: mt_random_experiment_cc591bd83d
English sentence: In our random experiment of casting a die, we introduced the notion of a *random variable*.
English term: random experiment
Version A: Trong «phép thử ngẫu nhiên» gieo xúc xắc của chúng ta, ta đã giới thiệu khái niệm *biến ngẫu nhiên*.
Version B: Trong «thí nghiệm ngẫu nhiên» gieo xúc xắc của chúng ta, ta đã giới thiệu khái niệm *biến ngẫu nhiên*.

### 38. item_id: mt_real_valued_scalars_e5f331e933
English sentence: consists of $n$ real-valued scalars,
English term: real-valued scalars
Version A: Trong ký hiệu toán học, nếu ta muốn nói rằng một vector $[1m{x}$ gồm $n$ «vô hướng thực», ta có thể biểu diễn điều này là $[1m{x} \in \mathbb{R}^n$.
Version B: Trong ký hiệu toán học, nếu ta muốn nói rằng một vector $[1m{x}$ gồm $n$ vô hướng có «giá trị thực», ta có thể biểu diễn là $[1m{x} \in \mathbb{R}^n$.

### 39. item_id: mt_regularization_term_b48f4963e7
English sentence: and the regularization term $s$.
English term: regularization term
Version A: Bước đầu tiên là tính các gradient của hàm mục tiêu $J=L+s$ theo hạng tử mất mát $L$ và «hạng tử chính quy hóa» $s$.
Version B: Bước đầu tiên là tính gradient của hàm mục tiêu $J=L+s$ theo hạng mất mát $L$ và «hạng chuẩn hóa» $s$.

### 40. item_id: mt_regularizing_0228b30aa3
English sentence: to regularizing statistical models
English term: regularizing
Version A: Trong :numref:`sec_weight_decay`, chúng ta đã giới thiệu cách tiếp cận cổ điển để «chuẩn hóa» mô hình thống kê bằng cách phạt chuẩn $L_2$ của trọng số.
Version B: Trong :numref:`sec_weight_decay`, chúng ta đã giới thiệu cách tiếp cận cổ điển để «điều chuẩn» các mô hình thống kê bằng cách phạt chuẩn $L_2$ của các trọng số.

### 41. item_id: mt_robotics_891e0758a7
English sentence: For instance, robotics, logistics, computational biology, particle physics, and astronomy owe some of their most impressive recent advances at least in parts to machine learning.
English term: robotics
Version A: Chẳng hạn, «robot học», logistics, sinh học tính toán, vật lý hạt và thiên văn học đều có một phần những tiến bộ ấn tượng gần đây nhất nhờ vào học máy.
Version B: Chẳng hạn, «người máy học», logistics, sinh học tính toán, vật lý hạt và thiên văn học đều có một phần những tiến bộ gần đây ấn tượng nhất nhờ học máy.

### 42. item_id: mt_row_vectors_04aadc236a
English sentence: Let us start off by visualizing the matrix $\mathbf{A}$ in terms of its row vectors
English term: row vectors
Version A: Hãy bắt đầu bằng cách biểu diễn ma trận $[1m{A}$ theo các «vector hàng» của nó
Version B: Hãy bắt đầu bằng cách biểu diễn ma trận $\mathbf{A}$ theo các «vectơ hàng» của nó

### 43. item_id: mt_rules_caa155adf8
English sentence: that we anticipate encountering, devising appropriate rules.
English term: rules
Version A: Để xây dựng bộ não của ứng dụng, chúng ta sẽ phải đi qua mọi trường hợp biên có thể gặp, và đề ra các «luật» phù hợp.
Version B: Để xây dựng bộ não của ứng dụng, chúng ta sẽ phải lần lượt xem xét mọi trường hợp biên có thể gặp, và đề ra các «quy tắc» phù hợp.

### 44. item_id: mt_self_driving_cars_ac724ed2e4
English sentence: ### Self-Driving Cars
English term: self-driving cars
Version A: ### «Ô tô tự lái»
Version B: ### «Xe tự lái»

### 45. item_id: mt_smoothness_fa9d205bd5
English sentence: Another useful notion of simplicity is smoothness,
English term: smoothness
Version A: Một khái niệm hữu ích khác về tính đơn giản là «tính trơn», tức là hàm không nên nhạy cảm với những thay đổi nhỏ trong đầu vào của nó.
Version B: Một khái niệm hữu ích khác về tính đơn giản là «độ trơn», tức là hàm không nên nhạy cảm với những thay đổi nhỏ ở đầu vào của nó.

### 46. item_id: mt_symmetry_18e1be4e79
English sentence: By symmetry, this also holds for $P(A, B) = P(A \mid B) P(B)$.
English term: symmetry
Version A: Theo «đối xứng», điều này cũng đúng với $P(A, B) = P(A \mid B) P(B)$.
Version B: Theo «tính đối xứng», điều này cũng đúng với $P(A, B) = P(A \mid B) P(B)$.

### 47. item_id: mt_tangent_line_fc8da8d99b
English sentence: This derivative is also the slope of the tangent line
English term: tangent line
Version A: Đạo hàm này cũng là hệ số góc của đường thẳng «tiếp tuyến» với đường cong $u = f(x)$ khi $x = 1$.
Version B: Đạo hàm này cũng là hệ số góc của «đường tiếp tuyến» với đường cong $u = f(x)$ khi $x = 1$.

### 48. item_id: mt_target_0e8a3ad980
English sentence: the *label* (or *target*).
English term: target
Version A: Trong các bài toán học có giám sát ở trên, thứ cần dự đoán là một thuộc tính đặc biệt được chỉ định là *nhãn* (hoặc *«biến mục tiêu»*).
Version B: Trong các bài toán học có giám sát ở trên, thứ cần dự đoán là một thuộc tính đặc biệt được chỉ định là *«nhãn»* (hoặc *mục tiêu*).

### 49. item_id: mt_target_distribution_e11bb1fcd8
English sentence: are drawn from the target distribution.
English term: target distribution
Version A: Với dịch chuyển hiệp biến, chúng ta giả định rằng $\\mathbf{x}_i$ với mọi $1 \leq i \leq n$ được lấy từ một phân phối nguồn nào đó và $\\mathbf{u}_i$ với mọi $1 \leq i \leq m$ được lấy từ «phân phối đích».
Version B: Với lệch hiệp biến, chúng ta giả sử rằng $\\mathbf{x}_i$ với mọi $1 \leq i \leq n$ được lấy từ một phân phối nguồn nào đó và $\\mathbf{u}_i$ với mọi $1 \leq i \leq m$ được lấy từ «phân phối mục tiêu».

### 50. item_id: mt_targets_4683e789c3
English sentence: Estimating targets given features is
English term: targets
Version A: Việc ước lượng các «mục tiêu» dựa trên các đặc trưng thường được gọi là *dự đoán* hoặc *suy luận*.
Version B: Việc ước lượng «nhãn mục tiêu» dựa trên đặc trưng thường được gọi là *dự đoán* hoặc *suy luận*.

### 51. item_id: mt_tensor_format_20bcb4d8d7
English sentence: we often begin with preprocessing raw data, rather than those nicely prepared data in the tensor format.
English term: tensor format
Version A: Để áp dụng học sâu vào việc giải quyết các vấn đề thực tế, chúng ta thường bắt đầu bằng việc tiền xử lý dữ liệu thô, thay vì những dữ liệu đã được chuẩn bị sẵn đẹp đẽ ở «dạng tensor».
Version B: Để áp dụng học sâu vào việc giải quyết các bài toán thực tế, chúng ta thường bắt đầu bằng việc tiền xử lý dữ liệu thô, thay vì những dữ liệu đã được chuẩn bị sẵn đẹp đẽ ở «định dạng tensor».

### 52. item_id: mt_test_accuracy_be3aebb55f
English sentence: Why might the test accuracy decrease after a while?
English term: test accuracy
Version A: Vì sao «độ chính xác kiểm tra» có thể giảm sau một thời gian?
Version B: Vì sao «độ chính xác trên tập kiểm tra» có thể giảm sau một thời gian?

### 53. item_id: mt_test_set_accuracy_98a76f479d
English sentence: as measured by test set accuracy
English term: test set accuracy
Version A: Đôi khi các mô hình dường như hoạt động tuyệt vời theo «độ chính xác trên tập kiểm tra» nhưng lại thất bại thảm hại khi triển khai do phân phối dữ liệu đột ngột thay đổi.
Version B: Đôi khi các mô hình dường như hoạt động tuyệt vời khi được đo bằng «độ chính xác tập kiểm tra» nhưng lại thất bại thảm hại khi triển khai khi phân phối của dữ liệu đột ngột dịch chuyển.

### 54. item_id: mt_training_example_bdb5992a4c
English sentence: Then for any training example $i$ with label $y_i$,
English term: training example
Version A: Khi đó với bất kỳ «mẫu huấn luyện» $i$ nào có nhãn $y_i$, chúng ta có thể lấy tỷ số giữa $p(y_i)/q(y_i)$ đã ước lượng để tính trọng số $\beta_i$, và đưa nó vào tối thiểu hóa rủi ro thực nghiệm có trọng số trong :eqref:`eq_weighted-empirical-risk-min`.
Version B: Khi đó với bất kỳ «ví dụ huấn luyện» $i$ nào có nhãn $y_i$, chúng ta có thể lấy tỉ số $p(y_i)/q(y_i)$ đã ước lượng để tính trọng số $\beta_i$, và đưa giá trị này vào tối thiểu hóa rủi ro thực nghiệm có trọng số trong :eqref:`eq_weighted-empirical-risk-min`.

### 55. item_id: mt_training_examples_79bcf24974
English sentence: for each constituent of a *batch* of training examples.
English term: training examples
Version A: Tuy nhiên, mặc dù những đối tượng kỳ lạ hơn này có xuất hiện trong học máy nâng cao (bao gồm [**trong học sâu**]), thường hơn (**khi chúng ta gọi backward trên một vector,**) chúng ta đang cố gắng tính các đạo hàm của các hàm mất mát cho từng phần tử của một *lô* «mẫu huấn luyện».
Version B: Tuy nhiên, mặc dù những đối tượng kỳ lạ hơn này có xuất hiện trong học máy nâng cao (bao gồm [**trong học sâu**]), thường hơn (**khi chúng ta gọi backward trên một vector,**) chúng ta đang cố tính các đạo hàm của các hàm mất mát cho từng phần tử cấu thành của một *batch* các «ví dụ huấn luyện».

### 56. item_id: mt_true_parameters_6636cf5533
English sentence: we know precisely what the true parameters are.
English term: true parameters
Version A: Trong trường hợp này, vì chúng ta tự tạo ra tập dữ liệu, nên chúng ta biết chính xác «tham số thực» là gì.
Version B: Trong trường hợp này, vì chúng ta tự tạo ra tập dữ liệu, nên chúng ta biết chính xác các «tham số thật» là gì.

### 57. item_id: mt_true_probability_0c04e78467
English sentence: Specifically, we calculate the relative frequency as the estimate of the true probability.
English term: true probability
Version A: Cụ thể, chúng ta tính tần suất tương đối như là ước lượng của «xác suất thực».
Version B: Cụ thể, chúng ta tính tần suất tương đối như là ước lượng của «xác suất thật».