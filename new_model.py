from datetime import datetime
import hashlib
import os.path
import random
import re
import struct
import sys
import tarfile
from tqdm import tqdm
import time
import os
import pickle

import numpy as np
from numpy import newaxis
from six.moves import urllib
import tensorflow as tf

from tensorflow.python.framework import graph_util
from tensorflow.python.framework import tensor_shape
from tensorflow.python.platform import gfile
from tensorflow.python.util import compat

import matplotlib.pyplot as plt

DATA_URL = 'http://download.tensorflow.org/models/image/imagenet/inception-2015-12-05.tgz'
BOTTLENECK_TENSOR_NAME = 'pool_3/_reshape:0'
BOTTLENECK_TENSOR_SIZE = 2048
MODEL_INPUT_WIDTH = 299
MODEL_INPUT_HEIGHT = 299
MODEL_INPUT_DEPTH = 3
JPEG_DATA_TENSOR_NAME = 'DecodeJpeg:0'
RESIZED_INPUT_TENSOR_NAME = 'ResizeBilinear:0'
MAX_NUM_IMAGES_PER_CLASS = 2 ** 27 - 1


def create_image_lists(image_dir, testing_percentage, validation_percentage):
    """이미지 디렉토리에서 인풋 데이터를 찾아 데이터로 변환한다"""

    ## image_dir가 존재하지 않는다면 오류 출력
    if not gfile.Exists(image_dir):
        print("Image directory '" + image_dir + "' not found.")
        return None

    result = {}

    ### image_dir 내 하위 디렉토리(label)를 가져온다
    sub_dirs = [x[0] for x in gfile.Walk(image_dir)]

    is_root_dir = True
    for sub_dir in sub_dirs:
        if is_root_dir:
            is_root_dir = False
            continue
        extensions = ['jpg', 'jpeg', 'JPG', 'JPEG','bin']

        file_list = []
        dir_name = os.path.basename(sub_dir)
        if dir_name == image_dir:
            continue

        print("Looking for images in '" + dir_name + "'")
        for extension in extensions:
            file_glob = os.path.join(image_dir, dir_name, '*.' + extension)
            file_list.extend(gfile.Glob(file_glob))

        ## 파일이 없거나 데이터가 작으면 예외 처리
        if not file_list:
            print('No files found')
            continue
        if len(file_list) < 20:
            print("WARNING: Folder has less than 20 images, which may cause issues.")
        elif len(file_list) > MAX_NUM_IMAGES_PER_CLASS:
            print("WARNING: Folder {} has more than {} images. Some images will never be selected".format(dir_name, MAX_NUM_IMAGES_PER_CLASS))
        label_name = re.sub(r'[^a-z0-9]+', ' ', dir_name.lower())

        ## 트레이닝 / 밸리데이션 / 테스트셋으로 나눈다.
        training_images = []
        testing_images = []
        validation_images = []
        for file_name in file_list:
            base_name = os.path.basename(file_name)

            hash_name = re.sub(r'_nohash_.*$', '', file_name)
            hash_name_hashed = hashlib.sha1(compat.as_bytes(hash_name)).hexdigest()
            percentage_hash = ((int(hash_name_hashed, 16) %
                               (MAX_NUM_IMAGES_PER_CLASS + 1)) *
                              (100.0 / MAX_NUM_IMAGES_PER_CLASS))

            if percentage_hash < validation_percentage:
                validation_images.append(base_name)
            elif percentage_hash < (testing_percentage + validation_percentage):
                testing_images.append(base_name)
            else:
                training_images.append(base_name)

        result[label_name] = {
            'dir': dir_name,
            'training': training_images,
            'testing': testing_images,
            'validation': validation_images,
        }

    return result


## 데이터를 다운로드받을 때 사용할 Tqdm 클래스를 정의한다.
class TqdmUpTo(tqdm):
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)



def maybe_download_and_extract():
    dest_directory = model_dir
    ensure_dir_exists(dest_directory)

    filename = DATA_URL.split("/")[-1]
    filepath = os.path.join(dest_directory, filename)

    if not os.path.exists(filepath):

        print("그래프 파일이 없습니다. 다운로드를 시작합니다.")

        with TqdmUpTo(unit='B', unit_scale=True, miniters=1, desc=DATA_URL) as t:
            urllib.request.urlretrieve(DATA_URL, filepath, reporthook=t.update_to, data=None)

        statinfo = os.stat(filepath)
        print("다운로드 완료: ", filename, statinfo.st_size, 'bytes.')
    else:
        print("그래프 파일이 이미 존재합니다.")
    tarfile.open(filepath, 'r:gz').extractall(dest_directory)


def create_inception_graph():
    """
    저장된 GraphDef 파일에서 그래프를 만들고
    Graph 오브젝트를 리턴한다.
    """
    with tf.Graph().as_default() as graph:
        model_filename = os.path.join(model_dir, 'classify_image_graph_def.pb')

        with gfile.FastGFile(model_filename, 'rb') as f:
            graph_def = tf.GraphDef()
            graph_def.ParseFromString(f.read())
            bottleneck_tensor, jpeg_data_tensor, resized_input_tensor = (
                tf.import_graph_def(graph_def, name='', return_elements=[
                    BOTTLENECK_TENSOR_NAME, JPEG_DATA_TENSOR_NAME, RESIZED_INPUT_TENSOR_NAME]))
    return graph, bottleneck_tensor, jpeg_data_tensor, resized_input_tensor


def should_distort_images(flip_left_right, random_crop, random_scale, random_brightness):
    """이미지 데이터에 변화를 줄지 결정한다."""
    return (flip_left_right or (random_crop != 0) or (random_scale != 0) or (random_brightness != 0))

def ensure_dir_exists(dir_name):
    if not os.path.exists(dir_name):
        os.makedirs(dir_name)


def cache_bottlenecks(sess, image_lists, image_dir, bottleneck_dir, jpeg_data_tensor, bottleneck_tensor):
    how_many_bottlenecks = 0
    ensure_dir_exists(bottleneck_dir)
    for label_name, label_lists in image_lists.items():
        for category in ['training', 'testing', 'validation']:
            category_list = label_lists[category]
            for index, unused_base_name in enumerate(category_list):
                get_or_create_bottleneck(sess, image_lists, label_name, index, image_dir, category, \
                                         bottleneck_dir, jpeg_data_tensor, bottleneck_tensor)
                how_many_bottlenecks += 1
                if how_many_bottlenecks % 100 == 0:
                    print('{} bottleneck files created'.format(how_many_bottlenecks))



def get_or_create_bottleneck(sess, image_lists, label_name, index, image_dir, category, \
                             bottleneck_dir, jpeg_data_tensor, bottleneck_tensor):
    label_lists = image_lists[label_name]
    sub_dir = label_lists['dir']
    sub_dir_path = os.path.join(bottleneck_dir, sub_dir)
    ensure_dir_exists(sub_dir_path)
    bottleneck_path = get_bottleneck_path(image_lists, label_name, index,
                                    bottleneck_dir, category)
    if not os.path.exists(bottleneck_path):
        create_bottleneck_file(bottleneck_path, image_lists, label_name, index,
                               image_dir, category, sess, jpeg_data_tensor,
                               bottleneck_tensor)
    with open(bottleneck_path, 'r') as bottleneck_file:
        bottleneck_string = bottleneck_file.read()

    did_hit_error = False
    try:
        bottleneck_values = [float(x) for x in bottleneck_string.split(',')]
    except ValueError:
        print('Invalid float found, recreating bottleneck')
        did_hit_error = True
    if did_hit_error:
        create_bottleneck_file(bottleneck_path, image_lists, label_name, index,
                               image_dir, category, sess, jpeg_data_tensor,
                               bottleneck_tensor)
        with open(bottleneck_path, 'r') as bottleneck_file:
            bottleneck_string = bottleneck_file.read()
        # Allow exceptions to propagate here, since they shouldn't happen after a
        # fresh creation
        bottleneck_values = [float(x) for x in bottleneck_string.split(',')]
    return bottleneck_values



def get_bottleneck_path(image_lists, label_name, index, bottleneck_dir, category):
    return get_image_path(image_lists, label_name, index, bottleneck_dir, category) + '.txt'


def get_image_path(image_lists, label_name, index, image_dir, category):

    if label_name not in image_lists:
        tf.logging.fatal('Label does not exist %s.', label_name)
    label_lists = image_lists[label_name]

    if category not in label_lists:
        tf.logging.fatal('Category does not exist %s.', category)
    category_list = label_lists[category]

    if not category_list:
        tf.logging.fatal('Label %s has no images in the category %s.', label_name, category)

    mod_index = index % len(category_list)
    base_name = category_list[mod_index]
    sub_dir = label_lists['dir']
    full_path = os.path.join(image_dir, sub_dir, base_name)

    return full_path

def min_max_scaling_ch1(arr):
	"""
	array : 2d array for ch1
	max value : 987
	min value : 142
	"""
	max_val = 987
	min_val = 142
	re_arr = (arr - max_val) / (max_val - min_val)

	return re_arr

def create_bottleneck_file(bottleneck_path, image_lists, label_name, index, image_dir,
                          category, sess, jpeg_data_tensor, bottleneck_tensor):
    print('보틀넥 파일 생성 시작 - {}'.format(bottleneck_path))
    image_path = get_image_path(image_lists, label_name, index, image_dir, category)

    if not gfile.Exists(image_path):
        tf.logging.fata('File does nto exist %s', image_path)
    # image_data = gfile.FastGFile(image_path, 'rb').read()
    ##############################################입력 데이터 수정 부분#################################################################
    ################################pickle 불러와서 array로 넣는 부분################################
    ################################concatenate 하는것도 여기서 진행 ################################
    image_data = pickle.load(open(image_path, 'rb'))
    # image_data = min_max_scaling_ch1(image_data)
    image_data = image_data[:, :, newaxis]
    image_data = np.concatenate((image_data, image_data, image_data), axis = 2)
    print(image_data.shape)


    try:
        bottleneck_values = run_bottleneck_on_image(
            sess, image_data, jpeg_data_tensor, bottleneck_tensor)
    except:
        raise RuntimeError('파일 처리 중 에러 발생: %s' % image_path)

    bottleneck_string = ','.join(str(x) for x in bottleneck_values)
    with open(bottleneck_path, 'w') as bottleneck_file:
        bottleneck_file.write(bottleneck_string)



def run_bottleneck_on_image(sess, image_data, image_data_tensor, bottleneck_tensor):
    bottleneck_values = sess.run(
        bottleneck_tensor, {image_data_tensor: image_data})
    bottleneck_values = np.squeeze(bottleneck_values)
    return bottleneck_values



####################################################  FC layer  #######################################################################
def add_final_training_ops(class_count, final_tensor_name, bottleneck_tensor):
    with tf.name_scope('input'):
        bottleneck_input = tf.placeholder_with_default(
            bottleneck_tensor, shape=[None, BOTTLENECK_TENSOR_SIZE],
            name='BottleneckInputPlaceholder')

        ground_truth_input = tf.placeholder(tf.float32, [None, class_count], name='GroundTruthInput')


    with tf.name_scope('FC_layer'):

        he_init = tf.contrib.layers.variance_scaling_initializer()
        dropout_while_training = tf.placeholder_with_default(True, shape= (), name = 'dropout_while_training')# 드롭아웃 스위치
        dropout_rate = 0.2

        X_drop = tf.layers.dropout(bottleneck_input, dropout_rate, training = dropout_while_training)
        hidden1 = tf.layers.dense(X_drop, units = 1000, activation = tf.nn.elu, name = 'hidden1', kernel_initializer = he_init)
        hidden1_drop = tf.layers.dropout(hidden1, dropout_rate, training = dropout_while_training)

        hidden2 = tf.layers.dense(hidden1_drop, units = 1000, activation = tf.nn.elu, name = 'hidden2', kernel_initializer = he_init)
        hidden2_drop = tf.layers.dropout(hidden2, dropout_rate, training = dropout_while_training)

        hidden3 = tf.layers.dense(hidden2_drop, units = 1000, activation = tf.nn.elu, name = 'hidden3', kernel_initializer = he_init)
        hidden3_drop = tf.layers.dropout(hidden3, dropout_rate, training = dropout_while_training)

        hidden4 = tf.layers.dense(hidden3_drop, units = 500, activation = tf.nn.elu, name = 'hidden4', kernel_initializer = he_init)
        hidden4_drop = tf.layers.dropout(hidden4, dropout_rate, training = dropout_while_training)

        logits = tf.layers.dense(inputs = hidden4_drop, units = 6)


    final_tensor = tf.nn.softmax(logits, name=final_tensor_name)

    with tf.name_scope('cross_entropy'):
        cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
            labels=ground_truth_input, logits=logits)
        with tf.name_scope('total'):
            cross_entropy_mean = tf.reduce_mean(cross_entropy)

    with tf.name_scope('train'):
        optimizer = tf.train.AdamOptimizer(learning_rate)
        train_step = optimizer.minimize(cross_entropy_mean)

    return (train_step, cross_entropy_mean, bottleneck_input, ground_truth_input, final_tensor)

#####################################################################################################################################


def add_evaluation_step(result_tensor, ground_truth_tensor):
    with tf.name_scope('accuracy'):
        with tf.name_scope('correct_prediction'):
            prediction = tf.argmax(result_tensor, 1)
            correct_prediction = tf.equal(
                prediction, tf.argmax(ground_truth_tensor, 1))
        with tf.name_scope('accuracy'):
            evaluation_step = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
    return evaluation_step, prediction




def get_random_cached_bottlenecks(sess, image_lists, how_many, category, bottleneck_dir, image_dir,
                                 jpeg_data_tensor, bottleneck_tensor):
    class_count = len(image_lists.keys())
    bottlenecks = []
    ground_truths = []
    filenames = []
    if how_many >= 0:
        # 샘플링한 보틀넥을 가져온다.
        for unused_i in range(how_many):
            label_index = random.randrange(class_count)
            label_name = list(image_lists.keys())[label_index]
            image_index = random.randrange(MAX_NUM_IMAGES_PER_CLASS + 1)
            image_name = get_image_path(image_lists, label_name, image_index,
                                      image_dir, category)
            bottleneck = get_or_create_bottleneck(sess, image_lists, label_name,
                                                image_index, image_dir, category,
                                                bottleneck_dir, jpeg_data_tensor,
                                                bottleneck_tensor)
            ground_truth = np.zeros(class_count, dtype=np.float32)
            ground_truth[label_index] = 1.0
            bottlenecks.append(bottleneck)
            ground_truths.append(ground_truth)
            filenames.append(image_name)
    else:
        # 보틀넥을 모두 가져온다.
        for label_index, label_name in enumerate(image_lists.keys()):
            for image_index, image_name in enumerate(image_lists[label_name][category]):
                image_name = get_image_path(image_lists, label_name, image_index,
                                            image_dir, category)
                bottleneck = get_or_create_bottleneck(sess, image_lists, label_name,
                                                      image_index, image_dir, category,
                                                      bottleneck_dir, jpeg_data_tensor,
                                                      bottleneck_tensor)
                ground_truth = np.zeros(class_count, dtype=np.float32)
                ground_truth[label_index] = 1.0
                bottlenecks.append(bottleneck)
                ground_truths.append(ground_truth)
                filenames.append(image_name)
    return bottlenecks, ground_truths, filenames




##############################################하이퍼파라미터 설정####################################################
image_dir = 'data'
output_graph = '/tmp/output_graph_ch2.pb'
output_labels = '/tmp/output_labels_ch2.txt'
summaries_dir = '/tmp/retrain_logs_ch2'
how_many_training_steps = 1000
learning_rate = 0.0001
testing_percentage = 25
validation_percentage = 25
eval_step_interval = 10
train_batch_size = 200
test_batch_size = -1
validation_batch_size = 200
print_misclassified_test_images = False
model_dir = '/tmp/imagenet_ch2'
bottleneck_dir = '/tmp/bottleneck_ch2'
final_tensor_name = 'final_result'
flip_left_right = False
random_crop = 0
random_scale = 0
random_brightness = 0
log_frequency = 10
log_device_placement = False

dropout_while_training = tf.placeholder_with_default(True, shape= (), name = 'dropout_while_training') # dropout중에 어덯게 할건지.
dropout_while_testing = tf.placeholder_with_default(False, shape= (), name = 'dropout_while_testing')



## inception_v3를 다운받아 압축을 푼다.
maybe_download_and_extract()

## 그래프와 보틀넥 텐서, 이미지데이터 텐서, 리사이즈 이미지 텐서를 불러온다.
graph, bottleneck_tensor, jpeg_data_tensor, resize_image_tensor = (create_inception_graph())

## 재학습할 폴더를 가져와서 레이블화한다.
image_lists = create_image_lists(image_dir, testing_percentage, validation_percentage)

class_count = len(image_lists.keys())

if class_count == 0:
    print('이미지가 해당 경로에 없습니다: ' + image_dir)

elif class_count == 1:
    print('해당 경로에 클래스가 1개만 발견되었습니다: ' + image_dir + ' - 분류를 위해 2개 이상의 클래스가 필요합니다.')

else:
    print("클래스가 2개 이상 있습니다. 학습을 시작합니다.")


## Image distortion // 현재 설정: False
do_distort_images = should_distort_images(flip_left_right, random_crop, random_scale, random_brightness)






##################################### 모델 학습 시작 ###################################################################
# 텐서플로우 세션을 열고, 보틀넥 파일을 가져오고, Inception_v3 끝에 학습시킬 마지막 classifier 레이어를 붙인다.
# 이후 에폭을 돌면서 이미지를 넣어 Training / Validation Accuracy를 산출한다.
# 학습이 완료되면 테스트셋 이미지를 넣어 최종 테스트셋 정확도를 평가한다.

acc_list = []

with tf.Session(graph=graph) as sess:
    if do_distort_images:
        (distorted_jpeg_data_tensor, distorted_image_tensor) = add_input_distortion(
            flip_left_right, random_crop, random_scale, random_brightness)
    else:
        cache_bottlenecks(sess, image_lists, image_dir, bottleneck_dir, jpeg_data_tensor, bottleneck_tensor)

    ## 네트워크의 끝에 우리가 원하는 분류 레이어를 붙인다.
    (train_step, cross_entropy, bottleneck_input,
     ground_truth_input, final_tensor) = add_final_training_ops(len(image_lists.keys()),
                                                                final_tensor_name,
                                                                bottleneck_tensor)

    ## 정확도 평가를 위한 새로운 오퍼레이션
    evaluation_step, prediction = add_evaluation_step(final_tensor, ground_truth_input)

    ## 가중치 초기화
    init = tf.global_variables_initializer()
    sess.run(init)

    for i in range(how_many_training_steps):

        ## 보틀넥과 정답지를 준비한다.
        if do_distort_images:
            (train_bottlenecks, train_ground_truth) = get_random_distorted_bottlenecks(
                sess, image_lists, train_batch_size, 'training', image_dir, distorted_jpeg_data_tensor,
                distorted_image_tensor, resized_image_tensor, bottleneck_tensor)
        else:
            (train_bottlenecks, train_ground_truth, _) = get_random_cached_bottlenecks(
                sess, image_lists, train_batch_size, 'training', bottleneck_dir, image_dir,
                jpeg_data_tensor, bottleneck_tensor)


        ## 보틀넥과 정답지를 모델에 집어넣어 학습시킨다.
        _ = sess.run(
            [train_step],
            feed_dict={bottleneck_input: train_bottlenecks,
                      ground_truth_input: train_ground_truth})

        ## 특정 구간마다 트레이닝 정확도와 cross entropy 로그, 밸리데이션 정확도를 출력한다.
        is_last_step = (i + 1 == how_many_training_steps)
        if (i % eval_step_interval) == 0 or is_last_step:
            train_accuracy, cross_entropy_value = sess.run(
                [evaluation_step, cross_entropy],
                feed_dict = {bottleneck_input: train_bottlenecks,
                            ground_truth_input: train_ground_truth})

            print('%s: Step %d: Train accuracy = %.1f%%'% (datetime.now(), i, train_accuracy * 100))
            print('%s: Step %d: Cross entropy = %f' % (datetime.now(), i, cross_entropy_value))

            validation_bottlenecks, validation_ground_truth, _ = (
                get_random_cached_bottlenecks(
                    sess, image_lists, validation_batch_size, 'validation', bottleneck_dir,
                    image_dir, jpeg_data_tensor, bottleneck_tensor))

            validation_accuracy = sess.run(
                evaluation_step,
                feed_dict = {bottleneck_input: validation_bottlenecks,
                            ground_truth_input: validation_ground_truth})
            print('%s: Step %d: Validation accuracy = %.1f%% (N=%d)'% (datetime.now(), i,
                                                                       validation_accuracy * 100,
                                                                       len(validation_bottlenecks)))

            ## 시각화를 위해 로그를 한벌 더 저장한다.
            acc_list.append({"epoch": i, "train_accuracy": train_accuracy, "validation_accuracy": validation_accuracy})

    ## 테스트셋에 사용할 보틀넥과 정답지를 가져온다.
    test_bottlenecks, test_ground_truth, test_filenames = (
        get_random_cached_bottlenecks(sess, image_lists, test_batch_size, 'testing', bottleneck_dir,
                                     image_dir, jpeg_data_tensor, bottleneck_tensor))

    ## 테스트셋 정확도와 예측 분류값을 가져온다.
    dropout_while_training = dropout_while_testing # dropout 때문에 추가한 코드. test 시에는 training을 False로 바꿔줘야함.

    test_accuracy, predictions = sess.run(
        [evaluation_step, prediction],
        feed_dict={bottleneck_input: test_bottlenecks,
                  ground_truth_input: test_ground_truth})
    print('최종 학습 정확도 = %.1f%% (N=%d)' % (test_accuracy * 100, len(test_bottlenecks)))

    output_graph_def = graph_util.convert_variables_to_constants(
        sess, graph.as_graph_def(), [final_tensor_name])
    with gfile.FastGFile(output_graph, 'wb') as f:
        f.write(output_graph_def.SerializeToString())
    with gfile.FastGFile(output_labels, 'w') as f:
        f.write('\n'.join(image_lists.keys()) + '\n')




##################################### learning curve 그리기 #############################################################

import pandas as pd
acc_df = pd.DataFrame.from_dict(acc_list)
acc_df.set_index('epoch', inplace=True)

f, ax = plt.subplots(figsize=(10, 5))
acc_df.plot(ax=ax)
plt.show()

#####################################confusion_matrix 그리기 ###########################################################
print(predictions.shape)
test_ground_truth = np.argmax(test_ground_truth, 1)
test_ground_truth = np.array(test_ground_truth)

from sklearn.metrics import confusion_matrix
import itertools
def plot_confusion_matrix(cm, classes,
                          normalize=False,
                          title='Confusion matrix',
                          cmap=plt.cm.Blues):
    """
    This function prints and plots the confusion matrix.
    Normalization can be applied by setting `normalize=True`.
    """
    if normalize:
        cm = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]
        print("Normalized confusion matrix")
    else:
        print('Confusion matrix, without normalization')

    print(cm)

    plt.imshow(cm, interpolation='nearest', cmap=cmap)
    plt.title(title)
    plt.colorbar()
    tick_marks = np.arange(len(classes))
    plt.xticks(tick_marks, classes, rotation=45)
    plt.yticks(tick_marks, classes)

    fmt = '.2f' if normalize else 'd'
    thresh = cm.max() / 2.
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        plt.text(j, i, format(cm[i, j], fmt),
                 horizontalalignment="center",
                 color="white" if cm[i, j] > thresh else "black")

    plt.tight_layout()
    plt.ylabel('True label')
    plt.xlabel('Predicted label')




# Compute confusion matrix
class_names = ['1', '2', '3', '4', '5', '6']
cnf_matrix = confusion_matrix(test_ground_truth, predictions)#########
np.set_printoptions(precision=2)

# Plot non-normalized confusion matrix
plt.figure()
plot_confusion_matrix(cnf_matrix, classes=class_names, title='Confusion matrix, without normalization')

plt.show()
