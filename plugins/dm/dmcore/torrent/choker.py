"""Which interested peers get an unchoke slot: the best uploaders to us, plus one optimistic peer."""


def choose(interested, rates, slots, rng):
    """(regular, optimistic): the `slots` peers that sent us the most, and one more picked at random from
    the rest (None when there is nobody else). Ties are broken at random."""
    order = list(interested)
    rng.shuffle(order)
    order.sort(key=lambda p: rates.get(p, 0), reverse=True)
    regular = set(order[:slots])
    rest = order[slots:]
    return regular, (rng.choice(rest) if rest else None)
