from abc import ABC, abstractmethod
import skimage.io as skimage_io
import codecs
import os

import numpy as np

from calamari_ocr.utils import parallel_map, split_all_ext


class DataSet(ABC):
    def __init__(self, has_images, has_texts, skip_invalid=False, remove_invalid=True):
        """ Dataset that stores a list of raw images and corresponding labels.

        Parameters
        ----------
        has_images : bool
            this dataset contains images
        has_texts : bool
            this dataset contains texts
        skip_invalid : bool
            skip invalid files instead of throwing an Exception
        remove_invalid : bool
            remove invalid files, thus dont count them to possible error on this data set
        """
        self._samples = []
        super().__init__()
        self.loaded = False
        self.has_images = has_images
        self.has_texts = has_texts

        self.skip_invalid = skip_invalid
        self.remove_invalid = remove_invalid

        if not self.has_images and not self.has_texts:
            raise Exception("Empty data set is not allowed.")

    def __len__(self):
        """ Number of samples

        Returns
        -------
        int
            Number of samples
        """
        return len(self._samples)

    def samples(self):
        """ List of all samples

        Returns
        -------
        list of dict
            List of all samples

        """
        return self._samples

    def prediction_samples(self):
        """ Extract all images from this set

        Returns
        -------
        list of images

        """
        if not self.loaded:
            raise Exception("Dataset must be loaded to access its training samples")

        return [sample["image"] for sample in self._samples]

    def text_samples(self):
        """ Extract all texts from this set

        Returns
        -------
        list of str

        """
        if not self.loaded:
            raise Exception("Dataset must be loaded to access its text")

        return [sample["text"] for sample in self._samples]

    def train_samples(self, skip_empty=False):
        """ Extract both list of images and list of texts

        Parameters
        ----------
        skip_empty : bool
            do not add empty files

        Returns
        -------
        list of images
        list of str

        """
        if not self.loaded:
            raise Exception("Dataset must be loaded to access its training samples")

        data, text = [], []

        for sample in self._samples:
            if "text" not in sample:
                if skip_empty:
                    print("Skipping empty sample {}".format(sample["id"]))
                    continue

                raise Exception("Sample {} is not a train sample. "
                                "Maybe the corresponding txt file is missing".format(sample["id"]))

            data.append(sample["image"])
            text.append(sample["text"])

        return data, text

    def add_sample(self, sample):
        """ Add a sample

        Parameters
        ----------
        sample : dict
            The sample
        """
        if not isinstance(sample, dict):
            raise Exception("A sample is expected to be a dictionary")

        if "id" not in sample:
            raise Exception("A sample needs an id")

        self.loaded = False
        self._samples.append(sample)

    def load_samples(self, processes=1, progress_bar=False):
        """ Load the samples into the memory

        This is usefull if a FileDataset shall load its files.

        Parameters
        ----------
        processes : int
            number of processes to use for loading
        progress_bar : bool
            show a progress bar of the progress

        Returns
        -------
        list of samples
        """
        if self.loaded:
            return self._samples

        data = parallel_map(self._load_sample, self._samples, desc="Loading Dataset", processes=processes, progress_bar=progress_bar)

        invalid_samples = []
        for i, ((line, text), sample) in enumerate(zip(data, self._samples)):
            sample["image"] = line
            sample["text"] = text
            if self.has_images:
                # skip invalid imanges (e. g. corrupted or empty files)
                if line is None or (line.size == 0 or np.amax(line) == np.amin(line)):
                    if self.skip_invalid:
                        invalid_samples.append(i)
                        if line is None:
                            print("Empty data: Image at '{}' is None (possibly corrupted)".format(sample['id']))
                        else:
                            print("Empty data: Image at '{}' is empty".format(sample['id']))
                    else:
                        raise Exception("Empty data: Image at '{}' is empty".format(sample['id']))

        if self.remove_invalid:
            # remove all invalid samples (reversed order!)
            for i in sorted(invalid_samples, reverse=True):
                del self._samples[i]

        self.loaded = True

        return self._samples

    @abstractmethod
    def _load_sample(self, sample):
        """ Load a single sample

        Parameters
        ----------
        sample : dict
            the sample to load

        Returns
        -------
        image
        text

        """
        return np.zeros((0, 0)), None


class RawDataSet(DataSet):
    def __init__(self, images=None, texts=None):
        """ Create a dataset from memory

        Since this dataset already contains all data in the memory, this dataset may not be loaded

        Parameters
        ----------
        images : list of images
            the images of the dataset
        texts : list of str
            the texts of this dataset
        """
        super().__init__(has_images=images is not None, has_texts=texts is not None)

        if images is None and texts is None:
            raise Exception("Empty data set is not allowed. Both images and text files are None")

        if images is not None and texts is not None and len(images) == 0 and len(texts) == 0:
            raise Exception("Empty data set provided.")

        if texts is None or len(texts) == 0:
            if images is None:
                raise Exception("Empty data set.")

            # No gt provided, probably prediction
            texts = [None] * len(images)

        if images is None or len(images) == 0:
            if len(texts) is None:
                raise Exception("Empty data set.")

            # No images provided, probably evaluation
            images = [None] * len(texts)

        for i, (image, text) in enumerate(zip(images, texts)):
            self.add_sample({
                "image": image,
                "text": text,
                "id": str(i),
            })

        self.loaded = True

    def _load_sample(self, sample):
        raise Exception("Raw dataset is always loaded")


class FileDataSet(DataSet):
    def __init__(self, images=[], texts=[],
                 skip_invalid=False, remove_invalid=True,
                 non_existing_as_empty=False):
        """ Create a dataset from a list of files

        Images or texts may be empty to create a dataset for prediction or evaluation only.

        Parameters
        ----------
        images : list of str, optional
            image files
        texts : list of str, optional
            text files
        skip_invalid : bool, optional
            skip invalid files
        remove_invalid : bool, optional
            remove invalid files
        non_existing_as_empty : bool, optional
            tread non existing files as empty. This is relevant for evaluation a dataset
        """
        super().__init__(has_images=images is None or len(images) > 0,
                         has_texts=texts is None or len(texts) > 0,
                         skip_invalid=skip_invalid,
                         remove_invalid=remove_invalid)
        self._non_existing_as_empty = non_existing_as_empty

        if self.has_images and not self.has_texts:
            # No gt provided, probably prediction
            texts = [None] * len(images)

        if self.has_texts and not self.has_images:
            # No images provided, probably evaluation
            images = [None] * len(texts)

        for image, text in zip(images, texts):
            try:
                if image is None and text is None:
                    raise Exception("An empty data point is not allowed. Both image and text file are None")

                img_bn, text_bn = None, None
                if image:
                    img_path, img_fn = os.path.split(image)
                    img_bn, img_ext = split_all_ext(img_fn)

                    if not self._non_existing_as_empty and not os.path.exists(image):
                        raise Exception("Image at '{}' must exist".format(image))

                if text:
                    if not self._non_existing_as_empty and not os.path.exists(text):
                        raise Exception("Text file at '{}' must exist".format(text))

                    text_path, text_fn = os.path.split(text)
                    text_bn, text_ext = split_all_ext(text_fn)

                if image and text and img_bn != text_bn:
                    raise Exception("Expected image base name equals text base name but got '{}' != '{}'".format(
                        img_bn, text_bn
                    ))
            except Exception as e:
                if self.skip_invalid:
                    print("Invalid data: {}".format(e))
                    continue
                else:
                    raise e

            self.add_sample({
                "image_path": image,
                "text_path": text,
                "id": img_bn if image else text_bn,
            })

    def _load_sample(self, sample):
        return self._load_line(sample["image_path"]),\
               self._load_gt_txt(sample["text_path"])

    def _load_gt_txt(self, gt_txt_path):
        if gt_txt_path is None:
            return None

        if not os.path.exists(gt_txt_path):
            if self._non_existing_as_empty:
                return ""
            else:
                raise Exception("Text file at '{}' does not exist".format(gt_txt_path))

        with codecs.open(gt_txt_path, 'r', 'utf-8') as f:
            return f.read()

    def _load_line(self, image_path):
        if image_path is None:
            return None

        if not os.path.exists(image_path):
            if self._non_existing_as_empty:
                return np.zeros((1, 1))
            else:
                raise Exception("Image file at '{}' does not exist".format(image_path))

        try:
            img = skimage_io.imread(image_path, as_gray=True)
        except:
            return None

        return img
