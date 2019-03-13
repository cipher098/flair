import warnings
import logging
from pathlib import Path
from typing import List, Union

import torch
import torch.nn as nn

import flair.nn
import flair.embeddings
from flair.data import Dictionary, Sentence, Label
from flair.file_utils import cached_path
from flair.training_utils import convert_labels_to_one_hot, clear_embeddings


log = logging.getLogger('flair')


class TextClassifier(flair.nn.Model):
    """
    Text Classification Model
    The model takes word embeddings, puts them into an LSTM to obtain a text representation, and puts the
    text representation in the end into a linear layer to get the actual class label.
    The model can handle single and multi class data sets.
    """

    def __init__(self,
                 document_embeddings: flair.embeddings.DocumentEmbeddings,
                 label_dictionary: Dictionary,
                 multi_label: bool):

        super(TextClassifier, self).__init__()

        self.document_embeddings = document_embeddings
        self.label_dictionary: Dictionary = label_dictionary
        self.multi_label = multi_label

        self.document_embeddings: flair.embeddings.DocumentLSTMEmbeddings = document_embeddings

        self.decoder = nn.Linear(self.document_embeddings.embedding_length, len(self.label_dictionary))

        self._init_weights()

        if multi_label:
            self.loss_function = nn.BCELoss()
        else:
            self.loss_function = nn.CrossEntropyLoss()

        # auto-spawn on GPU if available
        self.to(flair.device)

    def _init_weights(self):
        nn.init.xavier_uniform_(self.decoder.weight)

    def forward(self, sentences) -> List[List[float]]:
        self.document_embeddings.embed(sentences)

        text_embedding_list = [sentence.get_embedding().unsqueeze(0) for sentence in sentences]
        text_embedding_tensor = torch.cat(text_embedding_list, 0).to(flair.device)

        label_scores = self.decoder(text_embedding_tensor)

        return label_scores

    def _get_state_dict(self):
        model_state = {
            'state_dict': self.state_dict(),
            'document_embeddings': self.document_embeddings,
            'label_dictionary': self.label_dictionary,
            'multi_label': self.multi_label,
        }
        return model_state

    def _init_model_with_state_dict(state):

        model = TextClassifier(
            document_embeddings=state['document_embeddings'],
            label_dictionary=state['label_dictionary'],
            multi_label=state['multi_label']
        )

        model.load_state_dict(state['state_dict'])
        return model

    def forward_loss(self, sentences: Union[List[Sentence], Sentence]) -> torch.tensor:
        scores = self.forward(sentences)
        return self._calculate_loss(scores, sentences)

    def forward_labels_and_loss(self, sentences: Union[Sentence, List[Sentence]]) -> (List[List[Label]], torch.tensor):
        scores = self.forward(sentences)
        labels = self._obtain_labels(scores)
        loss = self._calculate_loss(scores, sentences)
        return labels, loss

    def predict(self, sentences: Union[Sentence, List[Sentence]], mini_batch_size: int = 32) -> List[Sentence]:
        """
        Predicts the class labels for the given sentences. The labels are directly added to the sentences.
        :param sentences: list of sentences
        :param mini_batch_size: mini batch size to use
        :return: the list of sentences containing the labels
        """
        with torch.no_grad():
            if type(sentences) is Sentence:
                sentences = [sentences]

            filtered_sentences = self._filter_empty_sentences(sentences)

            batches = [filtered_sentences[x:x + mini_batch_size] for x in range(0, len(filtered_sentences), mini_batch_size)]

            for batch in batches:
                scores = self.forward(batch)
                predicted_labels = self._obtain_labels(scores)

                for (sentence, labels) in zip(batch, predicted_labels):
                    sentence.labels = labels

                clear_embeddings(batch)

            return sentences

    @staticmethod
    def _filter_empty_sentences(sentences: List[Sentence]) -> List[Sentence]:
        filtered_sentences = [sentence for sentence in sentences if sentence.tokens]
        if len(sentences) != len(filtered_sentences):
            log.warning('Ignore {} sentence(s) with no tokens.'.format(len(sentences) - len(filtered_sentences)))
        return filtered_sentences

    def _calculate_loss(self, scores: List[List[float]], sentences: List[Sentence]) -> float:
        """
        Calculates the loss.
        :param scores: the prediction scores from the model
        :param sentences: list of sentences
        :return: loss value
        """
        if self.multi_label:
            return self._calculate_multi_label_loss(scores, sentences)

        return self._calculate_single_label_loss(scores, sentences)

    def _obtain_labels(self, scores: List[List[float]]) -> List[List[Label]]:
        """
        Predicts the labels of sentences.
        :param scores: the prediction scores from the model
        :return: list of predicted labels
        """

        if self.multi_label:
            return [self._get_multi_label(s) for s in scores]

        return [self._get_single_label(s) for s in scores]

    def _get_multi_label(self, label_scores) -> List[Label]:
        labels = []

        sigmoid = torch.nn.Sigmoid()

        results = list(map(lambda x: sigmoid(x), label_scores))
        for idx, conf in enumerate(results):
            if conf > 0.5:
                label = self.label_dictionary.get_item_for_index(idx)
                labels.append(Label(label, conf.item()))

        return labels

    def _get_single_label(self, label_scores) -> List[Label]:
        conf, idx = torch.max(label_scores, 0)
        label = self.label_dictionary.get_item_for_index(idx.item())

        return [Label(label, conf.item())]

    def _calculate_multi_label_loss(self, label_scores, sentences: List[Sentence]) -> float:
        sigmoid = nn.Sigmoid()
        return self.loss_function(sigmoid(label_scores), self._labels_to_one_hot(sentences))

    def _calculate_single_label_loss(self, label_scores, sentences: List[Sentence]) -> float:
        return self.loss_function(label_scores, self._labels_to_indices(sentences))

    def _labels_to_one_hot(self, sentences: List[Sentence]):
        label_list = [sentence.get_label_names() for sentence in sentences]
        one_hot = convert_labels_to_one_hot(label_list, self.label_dictionary)
        one_hot = [torch.FloatTensor(l).unsqueeze(0) for l in one_hot]
        one_hot = torch.cat(one_hot, 0).to(flair.device)
        return one_hot

    def _labels_to_indices(self, sentences: List[Sentence]):
        indices = [
            torch.LongTensor([self.label_dictionary.get_idx_for_item(label.value) for label in sentence.labels])
            for sentence in sentences
        ]

        vec = torch.cat(indices, 0).to(flair.device)

        return vec

    def _fetch_model(model_name) -> str:

        model_map = {}
        aws_resource_path = 'https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/models-v0.4'

        model_map['de-offensive-language'] = '/'.join([aws_resource_path, 'TEXT-CLASSIFICATION_germ-eval-2018_task-1',
                                  'germ-eval-2018-task-1.pt'])

        model_map['en-sentiment'] = '/'.join([aws_resource_path, 'TEXT-CLASSIFICATION_imdb', 'imdb.pt'])

        cache_dir = Path('models')
        if model_name in model_map:
            model_name = cached_path(model_map[model_name], cache_dir=cache_dir)

        return model_name