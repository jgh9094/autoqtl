import warnings
from deap import tools, gp
from collections import defaultdict
from inspect import isclass
from filelock import Timeout

import numpy as np
from sklearn.metrics import check_scoring
from stopit import threading_timeoutable

from sklearn.model_selection import train_test_split

from .operator_utils import set_sample_weight
# One point crossover
def cxOnePoint(ind1, ind2):
    """Randomly select in each individual and exchange each subtree with the
    point as root between each individual.
    :param ind1: First tree participating in the crossover.
    :param ind2: Second tree participating in the crossover.
    :returns: A tuple of two trees.
    """
    # List all available primitive types in each individual
    types1 = defaultdict(list)
    types2 = defaultdict(list)

    for idx, node in enumerate(ind1[1:], 1):
        types1[node.ret].append(idx)
    common_types = []
    for idx, node in enumerate(ind2[1:], 1):
        if node.ret in types1 and node.ret not in types2:
            common_types.append(node.ret)
        types2[node.ret].append(idx)

    if len(common_types) > 0:
        type_ = np.random.choice(common_types)

        index1 = np.random.choice(types1[type_])
        index2 = np.random.choice(types2[type_])

        slice1 = ind1.searchSubtree(index1)
        slice2 = ind2.searchSubtree(index2)
        ind1[slice1], ind2[slice2] = ind2[slice2], ind1[slice1]

    return ind1, ind2


# point mutation function
def mutNodeReplacement(individual, pset):
    """Replaces a randomly chosen primitive from *individual* by a randomly
    chosen primitive no matter if it has the same number of arguments from the :attr:`pset`
    attribute of the individual.
    Parameters
    ----------
    individual: DEAP individual
        A list of pipeline operators and model parameters that can be
        compiled by DEAP into a callable function

    Returns
    -------
    individual: DEAP individual
        Returns the individual with one of point mutation applied to it

    """

    index = np.random.randint(0, len(individual))
    node = individual[index]
    slice_ = individual.searchSubtree(index)

    if node.arity == 0:  # Terminal
        term = np.random.choice(pset.terminals[node.ret])
        if isclass(term):
            term = term()
        individual[index] = term
    else:   # Primitive
        # find next primitive if any
        rindex = None
        if index + 1 < len(individual):
            for i, tmpnode in enumerate(individual[index + 1:], index + 1):
                if isinstance(tmpnode, gp.Primitive) and tmpnode.ret in node.args:
                    rindex = i
                    break

        # pset.primitives[node.ret] can get a list of the type of node
        # for example: if op.root is True then the node.ret is Output_DF object
        # based on the function _setup_pset. Then primitives is the list of classifor or regressor
        primitives = pset.primitives[node.ret]

        if len(primitives) != 0:
            new_node = np.random.choice(primitives)
            new_subtree = [None] * len(new_node.args)
            if rindex:
                rnode = individual[rindex]
                rslice = individual.searchSubtree(rindex)
                # find position for passing return values to next operator
                position = np.random.choice([i for i, a in enumerate(new_node.args) if a == rnode.ret])
            else:
                position = None
            for i, arg_type in enumerate(new_node.args):
                if i != position:
                    term = np.random.choice(pset.terminals[arg_type])
                    if isclass(term):
                        term = term()
                    new_subtree[i] = term
            # paste the subtree to new node
            if rindex:
                new_subtree[position:position + 1] = individual[rslice]
            # combine with primitives
            new_subtree.insert(0, new_node)
            individual[slice_] = new_subtree

    return individual,


# utility mutation function to use in varOr() function
def mutate_random_individual(population, toolbox):
    """Picks a random individual from the population, and performs mutation on a copy of it.

    Parameters
    ----------
    population: array of individuals

    Returns
    ----------
    individual: individual
        An individual which is a mutated copy of one of the individuals in population,
        the returned individual does not have fitness.values
    """
    idx = np.random.randint(0,len(population))
    ind = population[idx]
    ind, = toolbox.mutate(ind)
    del ind.fitness.values
    return ind


# utility function to choose two individuals for crossover, to be used in varOr() function
def pick_two_individuals_eligible_for_crossover(population):
    """Pick two individuals from the population which can do crossover, that is, they share a primitive.

    Parameters
    ----------
    population: array of individuals

    Returns
    ----------
    tuple: (individual, individual)
        Two individuals which are not the same, but share at least one primitive.
        Alternatively, if no such pair exists in the population, (None, None) is returned instead.
    """
    primitives_by_ind = [set([node.name for node in ind if isinstance(node, gp.Primitive)])
                         for ind in population]
    pop_as_str = [str(ind) for ind in population]

    eligible_pairs = [(i, i+1+j) for i, ind1_prims in enumerate(primitives_by_ind)
                                 for j, ind2_prims in enumerate(primitives_by_ind[i+1:])
                                 if not ind1_prims.isdisjoint(ind2_prims) and
                                    pop_as_str[i] != pop_as_str[i+1+j]]

    # Pairs are eligible in both orders, this ensures that both orders are considered
    eligible_pairs += [(j, i) for (i, j) in eligible_pairs]

    if not eligible_pairs:
        # If there are no eligible pairs, the caller should decide what to do
        return None, None

    pair = np.random.randint(0, len(eligible_pairs))
    idx1, idx2 = eligible_pairs[pair]

    return population[idx1], population[idx2]


# utility function varOr() to be used in the algorithm eaMuPlusLambda() function
def varOr(population, toolbox, lambda_, cxpb, mutpb):
    """Part of an evolutionary algorithm applying only the variation part
    (crossover, mutation **or** reproduction). The modified individuals have
    their fitness invalidated. The individuals are cloned so returned
    population is independent of the input population.
    :param population: A list of individuals to vary.
    :param toolbox: A :class:`~deap.base.Toolbox` that contains the evolution
                    operators.
    :param lambda\_: The number of children to produce
    :param cxpb: The probability of mating two individuals.
    :param mutpb: The probability of mutating an individual.
    :returns: The final population
    :returns: A class:`~deap.tools.Logbook` with the statistics of the
              evolution
    The variation goes as follow. On each of the *lambda_* iteration, it
    selects one of the three operations; crossover, mutation or reproduction.
    In the case of a crossover, two individuals are selected at random from
    the parental population :math:`P_\mathrm{p}`, those individuals are cloned
    using the :meth:`toolbox.clone` method and then mated using the
    :meth:`toolbox.mate` method. Only the first child is appended to the
    offspring population :math:`P_\mathrm{o}`, the second child is discarded.
    In the case of a mutation, one individual is selected at random from
    :math:`P_\mathrm{p}`, it is cloned and then mutated using using the
    :meth:`toolbox.mutate` method. The resulting mutant is appended to
    :math:`P_\mathrm{o}`. In the case of a reproduction, one individual is
    selected at random from :math:`P_\mathrm{p}`, cloned and appended to
    :math:`P_\mathrm{o}`.
    This variation is named *Or* beceause an offspring will never result from
    both operations crossover and mutation. The sum of both probabilities
    shall be in :math:`[0, 1]`, the reproduction probability is
    1 - *cxpb* - *mutpb*.
    """
    offspring = []

    for _ in range(lambda_):
        op_choice = np.random.random()
        if op_choice < cxpb:  # Apply crossover
            ind1, ind2 = pick_two_individuals_eligible_for_crossover(population)
            if ind1 is not None:
                ind1, _ = toolbox.mate(ind1, ind2)
                del ind1.fitness.values
            else:
                # If there is no pair eligible for crossover, we still want to
                # create diversity in the population, and do so by mutation instead.
                ind1 = mutate_random_individual(population, toolbox)
            offspring.append(ind1)
        elif op_choice < cxpb + mutpb:  # Apply mutation
            ind = mutate_random_individual(population, toolbox)
            offspring.append(ind)
        else:  # Apply reproduction
            idx = np.random.randint(0, len(population))
            offspring.append(toolbox.clone(population[idx]))

    return offspring



# utility function used in the algorithm eaMuPlusLambda() function
def initialize_stats_dict(individual):
    '''
    Initializes the stats dict for individual
    The statistics initialized are:
        'generation': generation in which the individual was evaluated. Initialized as: 0
        'mutation_count': number of mutation operations applied to the individual and its predecessor cumulatively. Initialized as: 0
        'crossover_count': number of crossover operations applied to the individual and its predecessor cumulatively. Initialized as: 0
        'predecessor': string representation of the individual. Initialized as: ('ROOT',)

    Parameters
    ----------
    individual: deap individual

    Returns
    -------
    object
    '''
    individual.statistics['generation'] = 0
    individual.statistics['mutation_count'] = 0
    individual.statistics['crossover_count'] = 0
    individual.statistics['predecessor'] = 'ROOT',


# eaMuPlusLambda algorithm for the GP
def eaMuPlusLambda(population, toolbox, mu, lambda_, cxpb, mutpb, ngen, pbar,
                   stats=None, halloffame=None, verbose=0,
                   per_generation_function=None, log_file=None):
    """This is the :math:`(\mu + \lambda)` evolutionary algorithm.
    :param population: A list of individuals.
    :param toolbox: A :class:`~deap.base.Toolbox` that contains the evolution
                    operators.
    :param mu: The number of individuals to select for the next generation.
    :param lambda\_: The number of children to produce at each generation.
    :param cxpb: The probability that an offspring is produced by crossover.
    :param mutpb: The probability that an offspring is produced by mutation.
    :param ngen: The number of generation.
    :param pbar: processing bar
    :param stats: A :class:`~deap.tools.Statistics` object that is updated
                  inplace, optional.
    :param halloffame: A :class:`~deap.tools.HallOfFame` object that will
                       contain the best individuals, optional.
    :param verbose: Whether or not to log the statistics.
    :param per_generation_function: if supplied, call this function before each generation
                            used by tpot to save best pipeline before each new generation
    :param log_file: io.TextIOWrapper or io.StringIO, optional (defaul: sys.stdout)
    :returns: The final population
    :returns: A class:`~deap.tools.Logbook` with the statistics of the
              evolution.
    The algorithm takes in a population and evolves it in place using the
    :func:`varOr` function. It returns the optimized population and a
    :class:`~deap.tools.Logbook` with the statistics of the evolution. The
    logbook will contain the generation number, the number of evalutions for
    each generation and the statistics if a :class:`~deap.tools.Statistics` is
    given as argument. The *cxpb* and *mutpb* arguments are passed to the
    :func:`varOr` function. The pseudocode goes as follow ::
        evaluate(population)
        for g in range(ngen):
            offspring = varOr(population, toolbox, lambda_, cxpb, mutpb)
            evaluate(offspring)
            population = select(population + offspring, mu)
    First, the individuals having an invalid fitness are evaluated. Second,
    the evolutionary loop begins by producing *lambda_* offspring from the
    population, the offspring are generated by the :func:`varOr` function. The
    offspring are then evaluated and the next generation population is
    selected from both the offspring **and** the population. Finally, when
    *ngen* generations are done, the algorithm returns a tuple with the final
    population and a :class:`~deap.tools.Logbook` of the evolution.
    This function expects :meth:`toolbox.mate`, :meth:`toolbox.mutate`,
    :meth:`toolbox.select` and :meth:`toolbox.evaluate` aliases to be
    registered in the toolbox. This algorithm uses the :func:`varOr`
    variation.
    """
    logbook = tools.Logbook()
    logbook.header = ['gen', 'nevals'] + (stats.fields if stats else [])

    # Initialize statistics dict for the individuals in the population, to keep track of mutation/crossover operations and predecessor relations
    for ind in population:
        initialize_stats_dict(ind)

    population[:] = toolbox.evaluate(population)

    record = stats.compile(population) if stats is not None else {}
    logbook.record(gen=0, nevals=len(population), **record)

    # Begin the generational process
    for gen in range(1, ngen + 1):
        # Vary the population
        offspring = varOr(population, toolbox, lambda_, cxpb, mutpb)


        # Update generation statistic for all individuals which have invalid 'generation' stats
        # This hold for individuals that have been altered in the varOr function
        for ind in offspring:
            if ind.statistics['generation'] == 'INVALID':
                ind.statistics['generation'] = gen

        # Evaluate the individuals with an invalid fitness
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]

        offspring = toolbox.evaluate(offspring)

        # Select the next generation population
        population[:] = toolbox.select(population + offspring, mu)

        # pbar process
        if not pbar.disable:
            # Print only the best individual fitness. Changed from TPOT, as AUTOQTL has two R2 values on two sets of data
            if verbose == 2:
                high_d1_score = max(halloffame.keys[x].wvalues[0] \
                    for x in range(len(halloffame.keys)))
                high_d2_score = max(halloffame.keys[x].wvalues[1]\
                    for x in range(len(halloffame.keys)))
                pbar.write('\nGeneration {0} - Current '
                            'best internal score on D1: {1} and on D2: {2}'.format(gen,
                                                        high_d1_score, high_d2_score),

                            file=log_file)

            # Print the entire Pareto front
            elif verbose == 3:
                pbar.write('\nGeneration {} - '
                            'Current Pareto front scores:'.format(gen),
                            file=log_file)
                for pipeline, pipeline_scores in zip(halloffame.items, reversed(halloffame.keys)):
                    pbar.write('\n{}\t{}\t{}'.format(
                            pipeline_scores.wvalues[0],
                            pipeline_scores.wvalues[1],
                            pipeline
                        ),
                        file=log_file
                    )

        # after each population save a periodic pipeline
        if per_generation_function is not None:
            per_generation_function(gen)

        # Update the statistics with the new population
        record = stats.compile(population) if stats is not None else {}
        logbook.record(gen=gen, nevals=len(invalid_ind), **record)

    return population, logbook

# Change to tpot's _wrapped_cross_val_score() as autoqtl does not do cross validation and also it has two sets of features & target for two inputted datasets
@threading_timeoutable(default="Timeout")
def _wrapped_score(sklearn_pipeline, features, target, scoring_function,
                    sample_weight=None):
    """Fit estimator and compute scores for a given dataset split.
    
    Parameters
    ----------
    sklearn_pipeline : pipeline object implementing 'fit'
        The object to use to fit the data.
    
    features : array-like of shape at least 2D
        The data to fit.
    
    target : array-like, optional, default: None
        The target variable to try to predict in the case of
        supervised learning.
    
    scoring_function : callable
        A scorer callable object / function with signature
        ``scorer(estimator, X, y)``.
    
    sample weight : array-like, optional
        List of sample weights to balance ( or un-balance) the dataset target as needed 
        
    Returns
    -------
    score : float
        The score of the pipeline
        
    """
    sample_weight_dict = set_sample_weight(sklearn_pipeline.steps, sample_weight)

    scorer = check_scoring(sklearn_pipeline, scoring=scoring_function)

    """features_train, features_test, target_train, target_test = train_test_split(features, target, test_size=0.2)
    sklearn_pipeline.fit(features_train, target_train, sample_weight_dict)
    pipeline_score = sklearn_pipeline.score(features_test, target_test)"""
    
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')

            score = scorer(sklearn_pipeline, features, target) # will return the result of sklearn pipeline score? Yes it does. Have to find a way to put in the sample_weight_dict
        return score
    
    except TimeoutError:
        return Timeout
    except Exception as e:
        return -float('inf')

