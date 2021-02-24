import logging
from typing import List

from chatty_goose.retrievers import Retriever
from chatty_goose.util import reciprocal_rank_fusion
from pygaggle.rerank.base import Query, Reranker, hits_to_texts
from pyserini.search import JSimpleSearcherResult, SimpleSearcher


__all__ = ["CQR"]


class CQR:
    """
    Converstional Query Reformulation passage retrieval module
    
    Parameters:
        searcher (SimpleSearcher): Pyserini searcher for Lucene index
        retrievers (List[Retriever]): List of retrievers to use for first-stage retrieval
        searcher_num_hits (int): number of hits returned by searcher - default 10
        early_fusion (bool): flag to perform fusion before second-stage retrieval - default True
        reranker (Reranker): optional reranker for second-stage retrieval
        reranker_query_index (int): retriever index to use for reranking query - defaults to last retriever
        reranker_query_retriever (Retriever): retriever for generating reranker query,
                                              overrides reranker_query_index if provided
    """

    def __init__(
        self,
        searcher: SimpleSearcher,
        retrievers: List[Retriever],
        searcher_num_hits: int = 10,
        early_fusion: bool = True,
        reranker: Reranker = None,
        reranker_query_index: int = -1,
        reranker_query_retriever: Retriever = None,
    ):
        self.searcher = searcher
        self.retrievers = retrievers
        self.searcher_num_hits = searcher_num_hits
        self.early_fusion = early_fusion
        self.reranker = reranker
        self.reranker_query_index = reranker_query_index
        self.reranker_query_retriever = reranker_query_retriever

    def retrieve(self, query) -> List[JSimpleSearcherResult]:
        retriever_hits = []
        retriever_queries = []
        for retriever in self.retrievers:
            new_query = retriever.rewrite(query)
            hits = self.searcher.search(new_query, k=int(self.searcher_num_hits))
            retriever_hits.append(hits)
            retriever_queries.append(new_query)

        # Merge results from multiple retrievers if required
        if self.early_fusion or self.reranker is None:
            retriever_hits = reciprocal_rank_fusion(retriever_hits)

        # Return results if no reranker
        if self.reranker is None:
            return retriever_hits

        # Get query for reranker
        if self.reranker_query_retriever is None:
            rerank_query = retriever_queries[self.reranker_query_index]
        else:
            rerank_query = self.reranker_query_retriever.rewrite(query)

        # Rerank results
        if self.early_fusion:
            results = self.rerank(rerank_query, retriever_hits)
        else:
            # Rerank all retriever results and fuse together
            results = []
            for hits in retriever_hits:
                results = self.rerank(rerank_query, hits)
            results = reciprocal_rank_fusion(results)
        return results

    def rerank(self, query, hits):
        if self.reranker is None:
            logging.info("Reranker not available, skipping reranking")
            return hits

        reranked = self.reranker.rerank(Query(query), hits_to_texts(hits))
        reranked_scores = [r.score for r in reranked]

        # Reorder hits with reranker scores
        reranked = list(zip(hits, reranked_scores))
        reranked.sort(key=lambda x: x[1], reverse=True)
        reranked_hits = [r[0] for r in reranked]
        return reranked_hits

    def reset_history(self):
        for retriever in self.retrievers:
            retriever.reset_history()

        if self.reranker_query_retriever:
            self.reranker_query_retriever.reset_history()
