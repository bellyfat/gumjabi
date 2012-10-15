from collections import OrderedDict

def create_indices(
    collection,
    indices,
    ):
    for index in indices:
        collection.ensure_index(index.items())

def safe_upsert(
    coll,
    _id,
    **kwargs
    ):
    if kwargs:
        coll.update(
            OrderedDict([
                ('_id', _id),
            ]),
            kwargs,
            upsert=True,
            safe=True,
        )
