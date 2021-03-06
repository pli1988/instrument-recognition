import numpy as np
import os
import cPickle
import pandas as pd
import yaml
import wave
import struct
import gc
from scipy.io import wavfile
from scipy.io import savemat


"""
This file contains all scripts necessary for preparing data.

The code in this file reads all wav files, metadata and annotations for mixed
tracks. And then it takes patches of x seconds each from each track and labels
them.
Finally the resulting raw data is saved to several mat files, each containing
x tracks.

WARNING: If save_size is set to 20 in prep_data(), it takes 2 to 10 min to
         read data for one mat file, 3GB memory to keep program running, and
         1.5GB disk storage to save one mat file.
         If you find yourself out of memory, set pickle_size to a lower value.
         Still looking for more efficient ways to store data.

Need discussion: Too many kinds of instruments (over 80) if use all
"""


def backup_wavfile_reader(fpath):
    """Read wav files when scipy wavfile fail to read.
    Args:
        fpath (str): path to the wav file to read
    Returns:
        numpy array: data read from wav file
    """
    f = wave.open(fpath, 'rb')
    res = []
    for i in xrange(f.getnframes()):
        frame = f.readframes(1)
        x = struct.unpack('=h', frame[:2])[0]
        y = struct.unpack('=h', frame[2:])[0]
        res.append([x, y])
    return np.array(res)


def read_mixed_from_files(dpath, dlist, pickle_file=None):
    """Read the mixed track files and return as dictionary
    Args:
        dpath (str): path to the directory "MedleyDB/Audio"
        dlist (list): list of str, each for one mixed track file
    Returns:
        dict: in the format of {song_name(string): song_data(numpy array)}
              song_data two rows n cols. Each row is a channel, each col is a
              time frame.
    """
    res = dict()
    for i in dlist:
        fpath = os.path.join(dpath, i, '{}_MIX.wav'.format(i))
        try:
            data = wavfile.read(fpath)[1].T
        except:
            print "Warning: can't read {}, switch to backup reader". \
                format(fpath)
            data = backup_wavfile_reader(fpath).T
        res[i] = data
    if pickle_file is not None:
        with open(pickle_file, 'w') as f:
            cPickle.dump(res, f)
    return res


def normalize_data(data):
    """Normalize data with respect to each file in place

    For each file, normalize each column using standardization

    Args:
        data (dict): in format of {song_name(string): song_data(numpy array)}
    Returns:
        N/A
    """
    for k in data.keys():
        mean = data[k].mean(axis=1).reshape(2, 1)
        std = data[k].std(axis=1).reshape(2, 1)
        data[k] = np.float32(((data[k] - mean) / std))


def read_activation_confs(path, pickle_file=None):
    """Read the annotation files of activation confidence, return as dictionary
    Args:
        path (string): path to the directory "MedleyDB"
    Returns:
        dict: in the format of {song_name(string): annotation(pandas df)}
    """
    dpath = os.path.join(path, 'Annotations', 'Instrument_Activations',
                         'ACTIVATION_CONF')
    dlist = os.listdir(dpath)
    res = dict()
    for i in dlist:
        fpath = os.path.join(dpath, i)
        annotation = pd.read_csv(fpath, index_col=False)
        k = i[:-20].split('(')[0]
        k = k.translate(None, "'-")
        res[k] = annotation
    if pickle_file is not None:
        with open(pickle_file, 'w') as f:
            cPickle.dump(res, f)
    return res


def read_meta_data(path, pickle_file=None):
    """Read the metadata for instrument info, return as dictionary
    Args:
        path (string): path to the directory "MedleyDB"
    Returns:
        dict: in the format of {song_name(string): instrument_map(dict)}
              instrument_map is of the format eg: {'S01': 'piano'}
    """
    dpath = os.path.join(path, "Audio")
    dlist = os.listdir(dpath)
    res = dict()
    for i in dlist:
        fpath = os.path.join(dpath, i, '{}_METADATA.yaml'.format(i))
        with open(fpath, 'r') as f:
            meta = yaml.load(f)
        instrument = {k: v['instrument'] for k, v in meta['stems'].items()}
        res[i] = instrument
    if pickle_file is not None:
        with open(pickle_file, 'w') as f:
            cPickle.dump(res, f)
    return res


def match_meta_annotation(meta, annotation):
    """Match instrument number in annotation with real instrument name in meta.

    Note: In the annotation of one mixed track, there can be multiple instances
          of the same instrument, in which case the same column name appears
          multiple times in the pandas df

    Args:
        meta (dict): in the format of {song_name(string): instrument_map(dict)}
                     instrument_map is of the format eg: {'S01': 'piano'}
        annotation (dict): {song_name(string): annotation(pandas df)}
    Returns:
        list: containing all instruments involved, sorted in alphebic order
    """
    assert(len(meta) == len(annotation))
    all_instruments = set()
    for k, v in annotation.items():
        v.rename(columns=meta[k], inplace=True)
        all_instruments.update(meta[k].values())
    return sorted(list(all_instruments))


def split_music_to_patches(data, annotation, inst_map, length=5):
    """Split each music file into (length) second patches and label each patch

    Note: for each music file, the last patch that is not long enough is
          abandoned.
          And each patch is raveled to have only one row.

    Args:
        data(dict): the raw input data for each music file
        annotation(dict): annotation for each music file
                          calculated as average confidence in this time period
        inst_map(dict): a dictionary that maps a intrument name to its correct
                        position in the sorted list of all instruments
        length(int): length of each patch, in seconds
    Returns:
        dict: {'X': np array for X, 'y': np array for y}
    """
    res = []
    patch_size = 44100 * length
    for k, v in data.items():
        for i, e in enumerate(xrange(0, v.shape[1] - patch_size, patch_size)):
            patch = v[:, e:patch_size+e].ravel()
            sub_df = annotation[k][(i * length <= annotation[k].time) &
                                   (annotation[k].time < (i + 1) * length)]
            inst_conf = sub_df.mean().drop('time')
            label = np.zeros(len(inst_map), dtype='float32')
            for j in inst_conf.index:
                temp = inst_conf[j]
                # if there are two columns of the same instrument, take maximum
                if isinstance(temp, pd.Series):
                    temp = temp.max()
                label[inst_map[j]] = temp
            res.append((patch, label))
    X, y = zip(*res)
    return {'X': np.array(X), 'y': np.array(y)}


def prep_data(in_path, out_path=os.curdir, save_size=20, start_from=0):
    """Prepare data for preprocessing
    Args:
        in_path(str): the path for "MedleyDB"
        out_path(str): the path to save pkl files, default to be current
        save_size(int): the number of wav files contained in each mat
                          file. Large save_size requires large memory
        start_from(int): the order of file in alphebic order to start reading
                         from. All files before that are ignored. Used to
                         continue from the file last read.
    Returns:
        N/A
    """

    # read annotations and match with metadata
    anno_pkl = os.path.join(out_path, 'anno_label.pkl')
    annotation = read_activation_confs(in_path)
    meta = read_meta_data(in_path)
    all_instruments = match_meta_annotation(meta, annotation)
    if not os.path.exists(anno_pkl):
        with open(anno_pkl, 'w') as f:
            cPickle.dump(annotation, f)

    # get a dictionary mapping all instrument to sorted order
    all_instruments_map = {e: i for i, e in enumerate(all_instruments)}

    # read mixed tracks
    dpath = os.path.join(in_path, "Audio")
    dlist = sorted(os.listdir(dpath))  # get list of tracks in sorted order
    for i in range(max(start_from, 0), len(dlist), save_size):
        tdlist = dlist[i:i+save_size]
        data = read_mixed_from_files(dpath, tdlist)
        print 'finished reading file'
        normalize_data(data)
        print 'finished normalizing data'
        # split to x second patches
        patched_data = split_music_to_patches(data, annotation,
                                              all_instruments_map)
        print 'finished taking patchs of data'
        del data
        # save patches to file
        patches_save_path = os.path.join(out_path, 'patch_data_{}_{}.mat'.
                                         format(i, i+save_size))
        if not os.path.exists(patches_save_path):
            savemat(patches_save_path, patched_data)
        del patched_data
        gc.collect()
        print 'finished {} of {}'.format(i+save_size, len(dlist))


def main():
    root = os.path.abspath(os.sep)
    in_path = os.path.join(root, 'Volumes', 'VOL2', 'MedleyDB')
    prep_data(in_path)
