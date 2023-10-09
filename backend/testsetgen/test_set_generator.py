from json import JSONDecodeError
import itertools
import asyncio
import glob
import os
from datetime import datetime
from tqdm.asyncio import tqdm as tqdm_asyncio

from langchain.chains import QAGenerationChain
from langchain.docstore.document import Document
from langchain.schema.embeddings import Embeddings

from backend.commons.prompts import QA_GENERATION_PROMPT_SELECTOR
from backend.utils import aload_and_chunk_docs, write_json, read_json
from backend.commons.configurations import QAConfigurations
from backend.commons.chroma import ChromaClient

import uuid
import logging

logger = logging.getLogger(__name__)


async def get_qa_from_chunk(
    chunk: Document,
    qa_generator_chain: QAGenerationChain,
) -> list[dict]:
    try:
        # return list of qa pairs
        qa_pairs = qa_generator_chain.run(chunk.page_content)

        # attach chunk metadata to qa_pair
        for qa_pair in qa_pairs:
            qa_pair["metadata"] = dict(**chunk.metadata)
            qa_pair["metadata"].update({"id": str(uuid.uuid4())})

        return qa_pairs
    except JSONDecodeError:
        return []


async def agenerate_eval_set_from_doc(
    hp: QAConfigurations,
    doc_path: str,
) -> list[dict[str, str]]:
    """Generate a pairs of QAs that are used as ground truth in downstream tasks, i.e. RAG evaluations

    Args:
        llm (BaseLanguageModel): the language model used in the QAGenerationChain
        chunks (List[Document]): the document chunks used for QA generation

    Returns:
        List[Dict[str, str]]: returns a list of dictionary of question - answer pairs
    """

    logger.debug(f"Starting QA generation process for {doc_path}.")

    # load data and chunk doc
    chunks = await aload_and_chunk_docs(hp, [doc_path])

    llm = hp.qa_generator_llm
    qa_generator_chain = QAGenerationChain.from_llm(
        llm, prompt=QA_GENERATION_PROMPT_SELECTOR.get_prompt(llm)
    )

    tasks = [get_qa_from_chunk(chunk, qa_generator_chain) for chunk in chunks]

    qa_pairs = await asyncio.gather(*tasks)
    qa_pairs = list(itertools.chain.from_iterable(qa_pairs))

    return qa_pairs


async def agenerate_eval_set_from_docs(
    hp: QAConfigurations,
    docs_path: list[str],
) -> list[dict]:
    """Asynchronous wrapper around the agenerate_eval_set function.

    Args:
        qa_gen_configs (dict): _description_
        docs_path (list[str]): _description_

    Returns:
        list[dict]: _description_
    """
    tasks = [agenerate_eval_set_from_doc(hp, doc_path) for doc_path in docs_path]

    results = await tqdm_asyncio.gather(*tasks)

    qa_pairs = list(itertools.chain.from_iterable(results))

    return qa_pairs


async def aupsert_embeddings_for_model(
    qa_pairs: list[dict], embedding_model: Embeddings
) -> None:
    with ChromaClient() as CHROMA_CLIENT:
        collection_name = embedding_model.model

        # check if collection already exists, if not create a new one with the embeddings
        if collection_name in [
            collection.name for collection in CHROMA_CLIENT.list_collections()
        ]:
            logger.info(f"Collection {collection_name} already exists, skipping it.")
            return None

        collection = CHROMA_CLIENT.create_collection(
            name=collection_name,
            metadata={
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            },
        )

        ids = [qa_pair["metadata"]["id"] for qa_pair in qa_pairs]

        embeddings = await embedding_model.aembed_documents(
            [qa_pair["answer"] for qa_pair in qa_pairs]
        )

        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=[
                {
                    "question": qa_pair["question"],
                    "answer": qa_pair["answer"],
                    **qa_pair["metadata"],
                }
                for qa_pair in qa_pairs
            ],
        )

    logger.info(f"Upserted {embedding_model.model} embeddings to vectorstore.")


async def agenerate_and_save_dataset(
    hp: QAConfigurations,
    docs_path: str,
    eval_dataset_path: str,
):
    """Generate a new evaluation dataset and save it to a JSON file."""

    logger.info("Starting QA generation suite.")

    # tarnsform list of list of dicts into list of dicts
    gt_dataset = await agenerate_eval_set_from_docs(hp, docs_path)

    # write eval dataset to json
    write_json(gt_dataset, eval_dataset_path)

    # cache answers of qa pairs in vectorstore for each embedding model in hyperparams list
    if hp.persist_to_vs:
        tasks = [
            aupsert_embeddings_for_model(gt_dataset, embedding_model)
            for embedding_model in hp.embedding_model_list
        ]

        await asyncio.gather(*tasks)


async def agenerate_evaluation_set(
    qa_gen_params_path: str, eval_dataset_path: str, document_store_path: str
):
    """Entry function to generate the evaluation dataset.

    Args:
        qa_gen_params (dict): _description_
        eval_dataset_path (str): _description_

    Returns:
        _type_: _description_
    """
    logger.info("Checking for evaluation dataset configs.")

    qa_gen_params = read_json(qa_gen_params_path)

    if isinstance(qa_gen_params, list):
        qa_gen_params = qa_gen_params[-1]

    # set up Hyperparameters objects at the beginning to evaluate inputs
    qa_gen_params = QAConfigurations.from_dict(qa_gen_params)

    document_store = glob.glob(f"{document_store_path}/*.pdf")

    # generate evaluation dataset
    if qa_gen_params.generate_eval_set or not os.path.exists(eval_dataset_path):
        if os.path.exists(eval_dataset_path):
            logger.info(
                "Existing evaluation dataset deleted due to 'generate_eval_set'=True."
            )
            os.remove(eval_dataset_path)

        # reset chromadb before
        with ChromaClient() as client:
            client.reset()

        await agenerate_and_save_dataset(
            qa_gen_params, document_store, eval_dataset_path
        )