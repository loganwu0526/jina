import os

import numpy as np
import pytest

from jina import Flow, Document
from jina.executors.indexers import BaseIndexer
from jina.executors.indexers.cache import DocIDCache
from jina.executors.indexers.keyvalue import BinaryPbIndexer
from jina.executors.indexers.vector import NumpyIndexer

cur_dir = os.path.dirname(os.path.abspath(__file__))

KV_IDX_FILENAME = 'kv_idx.bin'
VEC_IDX_FILENAME = 'vec_idx.bin'
DOCS_TO_SEARCH = 1
TOP_K = 5
REQUEST_SIZE = 4
DOCS_TO_INDEX = 10


def config_env(field, tmp_workspace, shards, indexers, polling):
    os.environ['JINA_SHARDS'] = str(shards)
    os.environ['JINA_CACHE_FIELD'] = field
    os.environ['JINA_POLLING'] = polling
    os.environ['JINA_TOPK'] = str(TOP_K)
    os.environ['JINA_TEST_CACHE_CRUD_WORKSPACE'] = str(tmp_workspace)
    os.environ['JINA_KV_IDX_NAME'] = KV_IDX_FILENAME.split('.bin')[0]
    os.environ['JINA_VEC_IDX_NAME'] = VEC_IDX_FILENAME.split('.bin')[0]
    if indexers == 'parallel':
        # the second indexer will be directly connected to entry gateway
        os.environ['JINA_KV_NEEDS'] = 'cache'
        os.environ['JINA_MERGER_NEEDS'] = '[vector, kv]'
    else:
        # else it requires to be in serial connection, after the first indexer
        os.environ['JINA_KV_NEEDS'] = 'vector'
        os.environ['JINA_MERGER_NEEDS'] = 'kv'


np.random.seed(0)
d_embedding = np.array([1, 1, 1, 1, 1, 1, 1])
c_embedding = np.array([2, 2, 2, 2, 2, 2, 2])


def get_documents(chunks, same_content, nr=10, index_start=0):
    next_chunk_id = nr + index_start
    for i in range(index_start, nr + index_start):
        with Document() as d:
            d.id = i
            if same_content:
                d.text = 'hello world'
                d.embedding = d_embedding
            else:
                d.text = f'hello world {i}'
                d.embedding = np.random.random(d_embedding.shape)
            for j in range(chunks):
                with Document() as c:
                    c.id = next_chunk_id
                    if same_content:
                        c.text = 'hello world from chunk'
                        c.embedding = c_embedding
                    else:
                        c.text = f'hello world from chunk {j}'
                        c.embedding = np.random.random(d_embedding.shape)

                next_chunk_id += 1
                d.chunks.append(c)
        yield d


def get_index_flow(field, tmp_path, shards, indexers):
    config_env(field, tmp_path, shards, indexers, polling='any')
    f = Flow.load_config(os.path.join(cur_dir, 'crud_cache_flow_index.yml'))
    return f


def get_query_flow(field, tmp_path, shards):
    # searching must always be sequential
    config_env(field, tmp_path, shards, 'sequential', polling='all')
    f = Flow.load_config(os.path.join(cur_dir, 'crud_cache_flow_query.yml'))
    return f


def get_delete_flow(field, tmp_path, shards, indexers):
    config_env(field, tmp_path, shards, indexers, polling='all')
    f = Flow.load_config(os.path.join(cur_dir, 'crud_cache_flow_index.yml'))
    return f


@pytest.mark.parametrize('chunks', [0, 3, 5])
@pytest.mark.parametrize('same_content', [False, True])
@pytest.mark.parametrize('nr', [0, 10, 100, 201])
def test_docs_generator(chunks, same_content, nr):
    chunk_content = None
    docs = list(get_documents(chunks=chunks, same_content=same_content, nr=nr))
    assert len(docs) == nr
    ids_used = set()
    check_docs(chunk_content, chunks, same_content, docs, ids_used)

    if nr > 0:
        index_start = 1 + max(list(ids_used))
    else:
        index_start = 1
    new_docs = list(get_documents(chunks=chunks, same_content=same_content, nr=nr, index_start=index_start))
    new_ids = set([d.id for d in new_docs])
    assert len(new_ids.intersection(ids_used)) == 0

    check_docs(chunk_content, chunks, same_content, new_docs, ids_used, index_start)


def check_docs(chunk_content, chunks, same_content, docs, ids_used, index_start=0):
    for i, d in enumerate(docs):
        i += index_start
        id_int = int(d.id)
        assert id_int not in ids_used
        ids_used.add(id_int)

        if same_content:
            assert d.text == 'hello world'
            np.testing.assert_almost_equal(d.embedding, d_embedding)
        else:
            assert d.text == f'hello world {i}'
            assert d.embedding.shape == d_embedding.shape

        assert len(d.chunks) == chunks

        for j, c in enumerate(d.chunks):
            id_int = int(c.id)
            assert id_int not in ids_used
            ids_used.add(id_int)
            if same_content:
                if chunk_content is None:
                    chunk_content = c.content_hash
                assert c.content_hash == chunk_content
                assert c.text == 'hello world from chunk'
                np.testing.assert_almost_equal(c.embedding, c_embedding)
            else:
                assert c.text == f'hello world from chunk {j}'
                assert c.embedding.shape == c_embedding.shape


def check_indexers_size(chunks, nr_docs, field, tmp_path, same_content, shards, post_op):
    cache_indexer_path = tmp_path / 'cache.bin'
    cache_full_size = 0
    with BaseIndexer.load(cache_indexer_path) as cache:
        assert isinstance(cache, DocIDCache)
        cache_full_size = cache.size
        print(f'cache size {cache.size}')

    for indexer_fname in [KV_IDX_FILENAME, VEC_IDX_FILENAME]:
        indexers_full_size = 0
        for i in range(shards):
            from jina.executors.compound import CompoundExecutor
            compound_name = 'inc_docindexer' if KV_IDX_FILENAME in indexer_fname else 'inc_vecindexer'
            workspace_folder = CompoundExecutor.get_component_workspace_from_compound_workspace(tmp_path,
                                                                                                compound_name,
                                                                                                i + 1 if shards > 1 else 0 )
            indexer_path = os.path.join(BaseIndexer.get_shard_workspace(workspace_folder=workspace_folder,
                                                                        workspace_name=indexer_fname.rstrip('.bin'),
                                                                        pea_id=i + 1 if shards > 1 else 0),
                                        f'{indexer_fname}')

            # in the configuration of content-hash / same_content=True
            # there aren't enough docs to satisfy batch size, only 1 shard will have it
            if os.path.exists(indexer_path):
                with BaseIndexer.load(indexer_path) as indexer:
                    if indexer_fname == KV_IDX_FILENAME:
                        assert isinstance(indexer, BinaryPbIndexer)
                    else:
                        assert isinstance(indexer, NumpyIndexer)
                    indexers_full_size += indexer.size

        if post_op == 'delete':
            assert indexers_full_size == 0
            assert cache_full_size == 0
        else:
            if field == 'content_hash' and same_content:
                if chunks > 0:
                    # one content from Doc, one from chunk
                    expected = 2
                    assert indexers_full_size == expected
                    assert cache_full_size == 2
                else:
                    assert indexers_full_size == 1
                    assert cache_full_size == 1
            else:
                nr_expected = (nr_docs + chunks * nr_docs) * 2 if post_op == 'index2' \
                    else nr_docs + chunks * nr_docs
                assert indexers_full_size == nr_expected
                assert cache_full_size == nr_expected


@pytest.mark.parametrize('indexers, field, shards, chunks, same_content',
                         [
                             ('sequential', 'id', 1, 5, False),
                             ('sequential', 'id', 3, 5, False),
                             ('sequential', 'id', 3, 5, True),
                             ('sequential', 'content_hash', 1, 0, False),
                             ('sequential', 'content_hash', 1, 0, True),
                             ('sequential', 'content_hash', 1, 5, False),
                             ('sequential', 'content_hash', 1, 5, True),
                             ('sequential', 'content_hash', 3, 5, True),
                             ('parallel', 'id', 3, 5, False),
                             ('parallel', 'id', 3, 5, True),
                             ('parallel', 'content_hash', 3, 5, False),
                             ('parallel', 'content_hash', 3, 5, True)
                         ])
def test_cache_crud(
        tmp_path,
        mocker,
        indexers,
        field,
        shards,
        chunks,
        same_content
):
    flow_index = get_index_flow(field=field, tmp_path=tmp_path, shards=shards, indexers=indexers)
    flow_query = get_query_flow(field=field, tmp_path=tmp_path, shards=shards)
    flow_delete = get_delete_flow(field=field, tmp_path=tmp_path, shards=shards, indexers=indexers)

    def validate_result_factory(num_matches):
        def validate_results(resp):
            mock()
            assert len(resp.docs) == DOCS_TO_SEARCH
            for d in resp.docs:
                matches = list(d.matches)
                # this differs depending on cache settings
                # it could be lower
                if num_matches != 0:
                    if field == 'content_hash' and same_content:
                        if chunks:
                            assert len(matches) == 2
                        else:
                            assert len(matches) == 1
                else:
                    assert len(matches) == num_matches

        return validate_results

    docs = list(get_documents(chunks=chunks, same_content=same_content, nr=DOCS_TO_INDEX))
    # ids in order to ensure no matches in KV
    search_docs = list(get_documents(chunks=0, same_content=False, nr=DOCS_TO_SEARCH, index_start=9999))

    # INDEX
    with flow_index as f:
        f.index(docs, request_size=REQUEST_SIZE)

    check_indexers_size(chunks, len(docs), field, tmp_path, same_content, shards, 'index')

    # INDEX (with new documents)
    chunks_ids = np.concatenate([d.chunks for d in docs])
    index_start_new_docs = 1 + max([int(d.id) for d in np.concatenate([chunks_ids, docs])])

    new_docs = list(get_documents(chunks=chunks, same_content=same_content, index_start=index_start_new_docs))
    with flow_index as f:
        f.index(new_docs, request_size=REQUEST_SIZE)

    check_indexers_size(chunks, len(docs), field, tmp_path, same_content, shards, 'index2')

    # QUERY
    mock = mocker.Mock()
    with flow_query as f:
        f.search(
            search_docs,
            on_done=validate_result_factory(TOP_K)
        )
    mock.assert_called_once()

    # UPDATE
    docs.extend(new_docs)
    del new_docs

    # id stays the same, we change the content
    for d in docs:
        d_content_hash_before = d.content_hash
        d.content = f'this is some new content for doc {d.id}'
        d.update_content_hash()
        assert d.content_hash != d_content_hash_before
        for chunk in d.chunks:
            c_content_hash_before = chunk.content_hash
            chunk.content = f'this is some new content for chunk {chunk.id}'
            chunk.update_content_hash()
            assert chunk.content_hash != c_content_hash_before

    with flow_index as f:
        f.update(docs)

    check_indexers_size(chunks, len(docs) / 2, field, tmp_path, same_content, shards, 'index2')

    # QUERY
    mock = mocker.Mock()
    with flow_query as f:
        f.search(
            search_docs,
            on_done=validate_result_factory(TOP_K)
        )
    mock.assert_called_once()

    # DELETE
    with flow_delete as f:
        f.delete(docs)

    check_indexers_size(chunks, 0, field, tmp_path, same_content, shards, 'delete')

    # QUERY
    mock = mocker.Mock()
    with flow_query as f:
        f.search(
            search_docs,
            on_done=validate_result_factory(0)
        )
    mock.assert_called_once()
