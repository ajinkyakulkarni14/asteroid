from itertools import permutations, combinations
import torch
from torch import nn
from scipy.optimize import linear_sum_assignment


class PITLossWrapper(nn.Module):
    r"""Permutation invariant loss wrapper.

    Args:
        loss_func: function with signature (targets, est_targets, **kwargs).
        pit_from (str): Determines how PIT is applied.

            * ``'pw_mtx'`` (pairwise matrix): `loss_func` computes pairwise
              losses and returns a torch.Tensor of shape
              :math:`(batch, n\_src, n\_src)`. Each element
              :math:`[batch, i, j]` corresponds to the loss between
              :math:`targets[:, i]` and :math:`est\_targets[:, j]`
            * ``'pw_pt'`` (pairwise point): `loss_func` computes the loss for
              a batch of single source and single estimates (tensors won't
              have the source axis). Output shape : :math:`(batch)`.
              See :meth:`~PITLossWrapper.get_pw_losses`.
            * ``'perm_avg'``(permutation average): `loss_func` computes the
              average loss for a given permutations of the sources and
              estimates. Output shape : :math:`(batch)`.
              See :meth:`~PITLossWrapper.best_perm_from_perm_avg_loss`.
            * ``'mix_it'``(mixture invariant): `loss_func` computes the
              loss for a given partition of the sources. Valid for any
              number of mixtures as soon as they contain the same number
              of sources.
              Output shape : :math:`(batch)`.
              See :meth:`~PITLossWrapper.best_part_mix_it`.
            * ``'mix_it_gen'``(mixture invariant generalized): `loss_func`
              computes the loss for a given partition of the sources.
              Valid only for two mixtures, but those mixtures do not
              necessarly have to contain the same number of sources.
              Output shape : :math:`(batch)`.
              See :meth:`~PITLossWrapper.best_part_mix_it_gen`.

            In terms of efficiency, ``'perm_avg'`` is the least efficicient.

        perm_reduce (Callable): torch function to reduce permutation losses.
            Defaults to None (equivalent to mean). Signature of the func
            (pwl_set, **kwargs) : (B, n_src!, n_src) --> (B, n_src!).
            `perm_reduce` can receive **kwargs during forward using the
            `reduce_kwargs` argument (dict). If those argument are static,
            consider defining a small function or using `functools.partial`.
            Only used in `'pw_mtx'` and `'pw_pt'` `pit_from` modes.

    For each of these modes, the best permutation and reordering will be
    automatically computed.

    Examples
        >>> import torch
        >>> from asteroid.losses import pairwise_neg_sisdr
        >>> sources = torch.randn(10, 3, 16000)
        >>> est_sources = torch.randn(10, 3, 16000)
        >>> # Compute PIT loss based on pairwise losses
        >>> loss_func = PITLossWrapper(pairwise_neg_sisdr, pit_from='pw_mtx')
        >>> loss_val = loss_func(est_sources, sources)
        >>>
        >>> # Using reduce
        >>> def reduce(perm_loss, src):
        >>>     weighted = perm_loss * src.norm(dim=-1, keepdim=True)
        >>>     return torch.mean(weighted, dim=-1)
        >>>
        >>> loss_func = PITLossWrapper(pairwise_neg_sisdr, pit_from='pw_mtx',
        >>>                            perm_reduce=reduce)
        >>> reduce_kwargs = {'src': sources}
        >>> loss_val = loss_func(est_sources, sources,
        >>>                      reduce_kwargs=reduce_kwargs)
    """

    def __init__(self, loss_func, pit_from="pw_mtx", perm_reduce=None):
        super().__init__()
        self.loss_func = loss_func
        self.pit_from = pit_from
        self.perm_reduce = perm_reduce
        if self.pit_from not in ['pw_mtx', 'pw_pt', 'perm_avg', 'mix_it', 'mix_it_gen']:
            raise ValueError(
                "Unsupported loss function type for now. Expected"
                "one of [`pw_mtx`, `pw_pt`, `perm_avg`, `mix_it`, `mix_it_gen`]"
            )

    def forward(self, est_targets, targets, return_est=False, reduce_kwargs=None, **kwargs):
        """Find the best permutation and return the loss.

        Args:
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of training targets
            return_est: Boolean. Whether to return the reordered targets
                estimates (To compute metrics or to save example).
            reduce_kwargs (dict or None): kwargs that will be passed to the
                pairwise losses reduce function (`perm_reduce`).
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            - Best permutation loss for each batch sample, average over
                the batch. torch.Tensor(loss_value)
            - The reordered targets estimates if return_est is True.
                torch.Tensor of shape [batch, nsrc, *].
        """
        n_src = targets.shape[1]
        assert n_src < 10, f"Expected source axis along dim 1, found {n_src}"
        if self.pit_from == "pw_mtx":
            # Loss function already returns pairwise losses
            pw_losses = self.loss_func(est_targets, targets, **kwargs)
        elif self.pit_from == "pw_pt":
            # Compute pairwise losses with a for loop.
            pw_losses = self.get_pw_losses(self.loss_func, est_targets, targets, **kwargs)
        elif self.pit_from == "perm_avg":
            # Cannot get pairwise losses from this type of loss.
            # Find best permutation directly.
            min_loss, batch_indices = self.best_perm_from_perm_avg_loss(
                self.loss_func, est_targets, targets, **kwargs
            )
            # Take the mean over the batch
            mean_loss = torch.mean(min_loss)
            if not return_est:
                return mean_loss
            reordered = self.reorder_source(est_targets, batch_indices)
            return mean_loss, reordered
        elif self.pit_from == 'mix_it':
            # Cannot get pairwise losses from this type of loss.
            # Find best permutation and return ordered sources directly.
            min_loss, reordered = self.best_part_mix_it(
                self.loss_func, est_targets, targets, **kwargs
            )
            # Take the mean over the batch
            mean_loss = torch.mean(min_loss)
            return mean_loss, reordered
        elif self.pit_from == 'mix_it_gen':
            # Cannot get pairwise losses from this type of loss.
            # Find best permutation and return ordered sources directly.
            min_loss, reordered = self.best_part_mix_it_generalized(
                self.loss_func, est_targets, targets, **kwargs
            )
            # Take the mean over the batch
            mean_loss = torch.mean(min_loss)
            return mean_loss, reordered
        else:
            return

        assert pw_losses.ndim == 3, (
            "Something went wrong with the loss " "function, please read the docs."
        )
        assert pw_losses.shape[0] == targets.shape[0], "PIT loss needs same batch dim as input"

        reduce_kwargs = reduce_kwargs if reduce_kwargs is not None else dict()
        min_loss, batch_indices = self.find_best_perm(
            pw_losses, perm_reduce=self.perm_reduce, **reduce_kwargs
        )
        mean_loss = torch.mean(min_loss)
        if not return_est:
            return mean_loss
        reordered = self.reorder_source(est_targets, batch_indices)
        return mean_loss, reordered

    @staticmethod
    def get_pw_losses(loss_func, est_targets, targets, **kwargs):
        """Get pair-wise losses between the training targets and its estimate
        for a given loss function.

        Args:
            loss_func: function with signature (targets, est_targets, **kwargs)
                The loss function to get pair-wise losses from.
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of training targets.
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            torch.Tensor or size [batch, nsrc, nsrc], losses computed for
            all permutations of the targets and est_targets.

        This function can be called on a loss function which returns a tensor
        of size [batch]. There are more efficient ways to compute pair-wise
        losses using broadcasting.
        """
        batch_size, n_src, *_ = targets.shape
        pair_wise_losses = targets.new_empty(batch_size, n_src, n_src)
        for est_idx, est_src in enumerate(est_targets.transpose(0, 1)):
            for target_idx, target_src in enumerate(targets.transpose(0, 1)):
                pair_wise_losses[:, est_idx, target_idx] = loss_func(est_src, target_src, **kwargs)
        return pair_wise_losses

    @staticmethod
    def best_perm_from_perm_avg_loss(loss_func, est_targets, targets, **kwargs):
        """Find best permutation from loss function with source axis.

        Args:
            loss_func: function with signature (targets, est_targets, **kwargs)
                The loss function batch losses from.
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of training targets.
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.Tensor`: The indices of the best permutations.
        """
        n_src = targets.shape[1]
        perms = torch.tensor(list(permutations(range(n_src))), dtype=torch.long)
        loss_set = torch.stack(
            [loss_func(est_targets[:, perm], targets, **kwargs) for perm in perms], dim=1
        )
        # Indexes and values of min losses for each batch element
        min_loss, min_loss_idx = torch.min(loss_set, dim=1)
        # Permutation indices for each batch.
        batch_indices = torch.stack([perms[m] for m in min_loss_idx], dim=0)
        return min_loss, batch_indices

     @staticmethod
    
    @staticmethod
    def best_part_mix_it(loss_func, est_targets, targets, **kwargs):
        """ Find best partition of the estimated sources that gives
            the minimum loss for the MixIT training paradigm in [1].
             Valid for any number of mixtures as soon as they contain
             the same number of sources.

        Args:
            loss_func: function with signature (targets, est_targets, **kwargs)
                The loss function batch losses from.
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of training targets.
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.LongTensor`: The indexes of the best permutations.

        References:
            [1] Scott Wisdom and Efthymios Tzinis and Hakan Erdogan and Ron J Weiss
            and Kevin Wilson and John R Hershey, "Unsupervised sound separation using
            mixtures of mixtures." arXiv preprint arXiv:2006.12701 (2020) $
        """

        # check input dimensions
        assert est_targets.shape[0] == targets.shape[0]
        assert est_targets.shape[2] == targets.shape[2]

        # get dimensions
        n_mixtures = targets.shape[1]        # number of mixtures 
        n_est = est_targets.shape[1]         # number of estimated sources
        if n_est % n_mixtures != 0:
            raise ValueError('The mixtures are assumed to contain the same number of sources')
        n_src = n_est // n_mixtures          # number of sources in each mixture

        # Generate all unique partitions of size k from a list lst of
        # length n, where l = n // k is the number of parts. The total
        # number of such partitions is: NPK(n,k) = n! / ((k!)^l * l!)
        # Algorithm recursively distributes items over parts
        def combs(lst, k, l):
            if l == 0:
                yield []
            else:
                for c in combinations(lst, k):
                    rest = [x for x in lst if x not in c]
                    for r in combs(rest, k, l-1):
                        yield [list(c), *r]

        # Generate all the possible partitions
        loss_set = []
        parts = list(combs(range(n_est), n_src, n_mixtures))     
        for partition in parts:
            assert len(partition[0]) == n_src
            assert len(partition) == n_mixtures
        
            # sum the sources according to the given partition
            est_mixes = torch.stack([torch.sum(est_targets[:, indexes, :], axis=1) for indexes in partition], axis=1)

            # get loss for the given partition
            loss_set.append(loss_func(est_mixes, targets, **kwargs)[:, None])
            
        loss_set = torch.cat(loss_set, dim=1)
        
        # Indexes and values of min losses for each batch element
        min_loss, min_loss_indexes = torch.min(loss_set, dim=1, keepdim=True)
        assert len(min_loss_indexes) == est_mixes.shape[0]

        # For each batch there is a different min_loss_idx
        ordered = torch.zeros_like(est_mixes)
        for b, idx in enumerate(min_loss_indexes):
            right_partition = parts[idx]
            # sum the estimated sources to get the estimated mixtures
            ordered[b, :, :] = torch.stack([torch.sum(est_targets[b, indexes, :][None, :, :], axis=1) for indexes in right_partition], axis=1)

        return min_loss, ordered

    @staticmethod
    def best_part_mix_it_generalized(loss_func, est_targets, targets, **kwargs):
        """ Find best partition of the estimated sources that gives
            the minimum loss for the MixIT training paradigm in [1].
            Valid only for two mixtures, but those mixtures do not
            necessarly have to contain the same number of sources.
            It is allowed the case where one mixture is silent.

        Args:
            loss_func: function with signature (targets, est_targets, **kwargs)
                The loss function batch losses from.
            est_targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of target estimates.
            targets: torch.Tensor. Expected shape [batch, nsrc, *].
                The batch of training targets.
            **kwargs: additional keyword argument that will be passed to the
                loss function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.LongTensor`: The indexes of the best permutations.

        References:
            [1] Scott Wisdom and Efthymios Tzinis and Hakan Erdogan and Ron J Weiss
            and Kevin Wilson and John R Hershey, "Unsupervised sound separation using
            mixtures of mixtures." arXiv preprint arXiv:2006.12701 (2020) $
        """

        # check input dimensions
        assert est_targets.shape[0] == targets.shape[0]
        assert est_targets.shape[2] == targets.shape[2]

        # get dimensions
        n_mixtures = targets.shape[1]        # number of mixtures 
        n_est = est_targets.shape[1]         # number of estimated sources

        if n_mixtures != 2:
            raise ValueError('Works only with two mixtures')

        # Generate all unique partitions of any size from a list lst of
        # length n. Algorithm recursively distributes items over parts
        def all_combinations(lst):
            all_combinations = []
            for k in range(len(lst) + 1):
                for c in combinations(lst, k):
                    rest = [x for x in lst if x not in c]
                    all_combinations.append([list(c), rest]) 
            return all_combinations

        # Generate all the possible partitions
        loss_set = []
        parts = all_combinations(range(n_est))    
        for partition in parts:
            assert len(partition) == n_mixtures
        
            # sum the sources according to the given partition
            est_mixes = torch.stack([torch.sum(est_targets[:, indexes, :], axis=1) for indexes in partition], axis=1)

            # get loss for the given partition
            loss_set.append(loss_func(est_mixes, targets, **kwargs)[:, None])

        loss_set = torch.cat(loss_set, dim=1)
            
        # Indexes and values of min losses for each batch element
        min_loss, min_loss_indexes = torch.min(loss_set, dim=1, keepdim=True)
        assert len(min_loss_indexes) == est_mixes.shape[0]

        # For each batch there is a different min_loss_idx
        ordered = torch.zeros_like(est_mixes)
        for b, idx in enumerate(min_loss_indexes):
            right_partition = parts[idx]
            # sum the estimated sources to get the estimated mixtures
            ordered[b, :, :] = torch.stack([torch.sum(est_targets[b, indexes, :][None, :, :], axis=1) for indexes in right_partition], axis=1)

        return min_loss, ordered

    @staticmethod
    def find_best_perm(pair_wise_losses, perm_reduce=None, **kwargs):
        """Find the best permutation, given the pair-wise losses.

        Dispatch between factorial method if number of sources is small (<3)
        and hungarian method for more sources. If `perm_reduce` is not None,
        the factorial method is always used.

        Args:
            pair_wise_losses (:class:`torch.Tensor`):
                Tensor of shape [batch, n_src, n_src]. Pairwise losses.
            perm_reduce (Callable): torch function to reduce permutation losses.
                Defaults to None (equivalent to mean). Signature of the func
                (pwl_set, **kwargs) : (B, n_src!, n_src) --> (B, n_src!)
            **kwargs: additional keyword argument that will be passed to the
                permutation reduce function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.Tensor`: The indices of the best permutations.
        """
        n_src = pair_wise_losses.shape[-1]
        if perm_reduce is not None or n_src <= 3:
            min_loss, batch_indices = PITLossWrapper.find_best_perm_factorial(
                pair_wise_losses, perm_reduce=perm_reduce, **kwargs
            )
        else:
            min_loss, batch_indices = PITLossWrapper.find_best_perm_hungarian(pair_wise_losses)
        return min_loss, batch_indices

    @staticmethod
    def reorder_source(source, batch_indices):
        """Reorder sources according to the best permutation.

        Args:
            source (torch.Tensor): Tensor of shape [batch, n_src, time]
            batch_indices (torch.Tensor): Tensor of shape [batch, n_src].
                Contains optimal permutation indices for each batch.

        Returns:
            :class:`torch.Tensor`:
                Reordered sources of shape [batch, n_src, time].
        """
        reordered_sources = torch.stack(
            [torch.index_select(s, 0, b) for s, b in zip(source, batch_indices)]
        )
        return reordered_sources

    @staticmethod
    def find_best_perm_factorial(pair_wise_losses, perm_reduce=None, **kwargs):
        """Find the best permutation given the pair-wise losses by looping
        through all the permutations.

        Args:
            pair_wise_losses (:class:`torch.Tensor`):
                Tensor of shape [batch, n_src, n_src]. Pairwise losses.
            perm_reduce (Callable): torch function to reduce permutation losses.
                Defaults to None (equivalent to mean). Signature of the func
                (pwl_set, **kwargs) : (B, n_src!, n_src) --> (B, n_src!)
            **kwargs: additional keyword argument that will be passed to the
                permutation reduce function.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.Tensor`: The indices of the best permutations.

        MIT Copyright (c) 2018 Kaituo XU.
        See `Original code
        <https://github.com/kaituoxu/Conv-TasNet/blob/master>`__ and `License
        <https://github.com/kaituoxu/Conv-TasNet/blob/master/LICENSE>`__.
        """
        n_src = pair_wise_losses.shape[-1]
        # After transposition, dim 1 corresp. to sources and dim 2 to estimates
        pwl = pair_wise_losses.transpose(-1, -2)
        perms = pwl.new_tensor(list(permutations(range(n_src))), dtype=torch.long)
        # Column permutation indices
        idx = torch.unsqueeze(perms, 2)
        # Loss mean of each permutation
        if perm_reduce is None:
            # one-hot, [n_src!, n_src, n_src]
            perms_one_hot = pwl.new_zeros((*perms.size(), n_src)).scatter_(2, idx, 1)
            loss_set = torch.einsum("bij,pij->bp", [pwl, perms_one_hot])
            loss_set /= n_src
        else:
            # batch = pwl.shape[0]; n_perm = idx.shape[0]
            # [batch, n_src!, n_src] : Pairwise losses for each permutation.
            pwl_set = pwl[:, torch.arange(n_src), idx.squeeze(-1)]
            # Apply reduce [batch, n_src!, n_src] --> [batch, n_src!]
            loss_set = perm_reduce(pwl_set, **kwargs)
        # Indexes and values of min losses for each batch element
        min_loss, min_loss_idx = torch.min(loss_set, dim=1)

        # Permutation indices for each batch.
        batch_indices = torch.stack([perms[m] for m in min_loss_idx], dim=0)
        return min_loss, batch_indices

    @staticmethod
    def find_best_perm_hungarian(pair_wise_losses: torch.Tensor):
        """Find the best permutation given the pair-wise losses, using the
        Hungarian algorithm.

        Args:
            pair_wise_losses (:class:`torch.Tensor`):
                Tensor of shape [batch, n_src, n_src]. Pairwise losses.

        Returns:
            tuple:
                :class:`torch.Tensor`: The loss corresponding to the best
                permutation of size (batch,).

                :class:`torch.Tensor`: The indices of the best permutations.
        """
        # After transposition, dim 1 corresp. to sources and dim 2 to estimates
        pwl = pair_wise_losses.transpose(-1, -2)
        # Just bring the numbers to cpu(), not the graph
        pwl_copy = pwl.detach().cpu()
        # Loop over batch + row indices are always ordered for square matrices.
        batch_indices = torch.tensor([linear_sum_assignment(pwl)[1] for pwl in pwl_copy]).to(
            pwl.device
        )
        min_loss = torch.gather(pwl, 2, batch_indices[..., None]).mean([-1, -2])
        return min_loss, batch_indices


class PITReorder(PITLossWrapper):
    """Permutation invariant reorderer. Only returns the reordered estimates.
    See `:py:class:asteroid.losses.PITLossWrapper`."""

    def forward(self, est_targets, targets, reduce_kwargs=None, **kwargs):
        _, reordered = super().forward(
            est_targets=est_targets,
            targets=targets,
            return_est=True,
            reduce_kwargs=reduce_kwargs,
            **kwargs,
        )
        return reordered
