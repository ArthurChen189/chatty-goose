import argparse
from chatty_goose.util import reciprocal_rank_fusion
import json
import time

from chatty_goose.models import HQE, T5_NTR
from pygaggle.rerank.base import Query, hits_to_texts
from pygaggle.rerank.transformer import MonoBERT
from pyserini.search import SimpleSearcher

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Fusion using HQE and NTR for CAsT.')
    parser.add_argument('--qid_queries', required=True, default='', help='query id - query mapping file')
    parser.add_argument('--output', required=True, default='', help='output file')
    parser.add_argument('--index', required=True, default='', help='index path')
    parser.add_argument('--hits', default=10, help='number of hits to retrieve')
    parser.add_argument('--rerank', action='store_true', default=False, help='rerank BM25 output using BERT')
    
    # See our MS MARCO documentation to understand how these parameter values were tuned.
    parser.add_argument('--k1', default=0.82, help='BM25 k1 parameter')
    parser.add_argument('--b', default=0.68, help='BM25 b parameter')
    parser.add_argument('--rm3', action='store_true', default=False, help='use RM3')
    parser.add_argument('--fbTerms', default=10, type=int, help='RM3 parameter: number of expansion terms')
    parser.add_argument('--fbDocs', default=10, type=int, help='RM3 parameter: number of documents')
    parser.add_argument('--originalQueryWeight', default=0.8, type=float, help='RM3 parameter: weight to assign to the original query')
    
    # HQE related hyperparameters. The default is tuned on CAsT train data
    parser.add_argument('--M0', default=5, type=int, help='aggregate historcial queries for first stage (BM25) retrieval')
    parser.add_argument('--M1', default=1, type=int, help='aggregate historcial queries for second stage (BERT) retrieval')
    parser.add_argument('--eta0', default=10, type=float, help='QPP threshold for first stage (BM25) retrieval')
    parser.add_argument('--eta1', default=12, type=float, help='QPP threshold for second stage (BERT) retrieval')
    parser.add_argument('--R0_topic', default=4.5, type=float, help='Topic keyword threshold for first stage (BM25) retrieval')
    parser.add_argument('--R1_topic', default=4, type=float, help='Topic keyword threshold for second stage (BERT) retrieval')
    parser.add_argument('--R0_sub', default=3.5, type=float, help='Subtopic keyword threshold for first stage (BM25) retrieval')
    parser.add_argument('--R1_sub', default=3, type=float, help='Subtopic keyword threshold for second stage (BERT) retrieval')
    parser.add_argument('--QR_method', default='hqe', help='input_query') #origin, concat, hqe
    parser.add_argument('--filter', default='pos', help='filter word method') #pos, no
    
    # Parameters for T5
    parser.add_argument('--max_length', default=64, help='T5 max sequence length')
    parser.add_argument('--num_beams', default=10, help='T5 number of beams')
    parser.add_argument('--early_stopping', action='store_true', default=True, help='T5 use early stopping')
    args = parser.parse_args()

    searcher = SimpleSearcher(args.index)
    searcher.set_bm25(float(args.k1), float(args.b))
    print("Initializing BM25, setting k1={} and b={}".format(args.k1, args.b))
    if args.rm3:
        searcher.set_rm3(args.fbTerms, args.fbDocs, args.originalQueryWeight)
        print(
            "Initializing RM3, setting fbTerms={}, fbDocs={} and originalQueryWeight={}".format(
                args.fbTerms, args.fbDocs, args.originalQueryWeight
            )
        )

    # Initialize reranker
    reranker = None
    if args.rerank:
        reranker = MonoBERT()

    HQE_for_BM25 = HQE(
        args.M0, args.eta0, args.R0_topic, args.R0_sub, args.filter, True
    )
    HQE_for_BERT = HQE(args.M1, args.eta1, args.R0_topic, args.R1_sub, args.filter)
    cqr = T5_NTR(
        model_name="castorini/t5-base-canard",
        max_length=args.max_length,
        num_beams=args.num_beams,
        early_stopping=args.early_stopping,
    )

    with open(args.output + ".tsv", "w") as fout0:
        with open(args.output + ".doc.tsv", "w") as fout1:

            total_query_count = 0
            with open(args.qid_queries) as json_file:
                data = json.load(json_file)

            qr_total_time = 0
            initial_time = time.time()
            for session in data:
                session_num = str(session["number"])

                start_time = time.time()
                for turn_id, conversations in enumerate(session["turn"]):
                    query = conversations["raw_utterance"]
                    total_query_count += 1

                    conversation_num = str(conversations["number"])
                    qid = session_num + "_" + conversation_num

                    qr_start_time = time.time()
                    qr_total_time += time.time() - qr_start_time

                    # Generate new queries
                    query_for_anserini = HQE_for_BM25.rewrite(query, searcher)
                    query_for_bert = HQE_for_BERT.rewrite(query, searcher)
                    rewritten_query = cqr.rewrite(query)

                    # Perform BM25 search
                    hits_1 = searcher.search(query_for_anserini, int(args.hits))
                    hits_2 = searcher.search(rewritten_query, int(args.hits))
                    hits = reciprocal_rank_fusion(hits_1, hits_2, k=60)

                    # Perform reranking using BERT
                    if reranker is not None:
                        texts = hits_to_texts(hits)
                        reranked = reranker.rerank(Query(rewritten_query), texts)
                        reranked_scores = [r.score for r in reranked]

                        # Reorder hits with reranker scores
                        reranked = list(zip(hits, reranked_scores))
                        reranked.sort(key=lambda x: x[1], reverse=True)
                        hits = [r[0] for r in reranked]

                    for rank in range(len(hits)):
                        docno = hits[rank].docid
                        fout0.write("{}\t{}\t{}\n".format(qid, docno, rank + 1))

                HQE_for_BM25.reset_history()
                HQE_for_BERT.reset_history()
                cqr.reset_history()
                time_per_query = (time.time() - start_time) / (turn_id + 1)
                print(
                    "Retrieving session {} with {} queries ({:0.3f} s/query)".format(
                        session["number"], turn_id + 1, time_per_query
                    )
                )

            time_per_query = (time.time() - initial_time) / (total_query_count)
            qr_time_per_query = qr_total_time / (total_query_count)
            print(
                "Retrieving {} queries ({:0.3f} s/query, QR {:0.3f} s/query)".format(
                    total_query_count, time_per_query, qr_time_per_query
                )
            )

    print("total Query Counts %d" % (total_query_count))
    print("Done!")
