from math import log, log10
from typing import List

import boa
from hypothesis import assume, event, note
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from hypothesis.strategies import integers

from contracts.main import CurveTwocryptoFactory as factory
from contracts.mocks import ERC20Mock as ERC20
from tests.utils.constants import UNIX_DAY
from tests.utils.strategies import address, pool_from_preset
from tests.utils.tokens import mint_for_testing


class StatefulBase(RuleBasedStateMachine):
    pool = None
    total_supply = 0
    coins = None
    balances = None
    decimals = None
    xcp_profit = 0
    xcp_profit_a = 0
    xcpx = 0
    depositors = None
    equilibrium = 0
    swapped_once = False
    fee_receiver = None
    admin = None

    @initialize(
        pool=pool_from_preset(),
        amount=integers(min_value=int(1e20), max_value=int(1e30)),
        user=address,
    )
    def initialize_pool(self, pool, amount, user):
        """Initialize the state machine with a pool and some
        initial liquidity.

        Prefer to use this method instead of the `__init__` method
        when initializing the state machine.
        """

        # cahing the pool generated by the strategy
        self.pool = pool

        # total supply of lp tokens (updated from reported balances)
        self.total_supply = 0

        # caching coins here for easier access
        self.coins = [ERC20.at(pool.coins(i)) for i in range(2)]

        # these balances should follow the pool balances
        self.balances = [0, 0]

        # cache the decimals of the coins
        self.decimals = [c.decimals() for c in self.coins]

        # initial profit is 1e18
        self.xcp_profit = 1e18
        self.xcp_profit_a = 1e18
        self.xcpx = 1e18

        self.depositors = set()

        self.equilibrium = 5e17

        self.swapped_once = False

        self.fee_receiver = factory.at(pool.factory()).fee_receiver()
        self.admin = factory.at(pool.factory()).admin()

        # figure out the amount of the second token for a balanced deposit
        balanced_amounts = self.get_balanced_deposit_amounts(amount)

        # correct amounts to the right number of decimals
        balanced_amounts = self.correct_all_decimals(balanced_amounts)

        note(
            "seeding pool with balanced amounts: {:.2e} {:.2e}".format(
                *balanced_amounts
            )
        )
        self.add_liquidity(balanced_amounts, user)
        note("[SUCCESS]")

    # --------------- utility methods ---------------

    def is_ramping(self) -> bool:
        """Check if the pool is currently ramping."""

        return self.pool.future_A_gamma_time() > boa.env.evm.patch.timestamp

    def correct_decimals(self, amount: int, coin_idx: int) -> int:
        """Takes an amount that uses 18 decimals and reduces its precision"""

        corrected_amount = int(
            amount // (10 ** (18 - self.decimals[coin_idx]))
        )
        # sometimes a non-zero amount generated
        # by the strategy is <= 0 when corrected
        if amount > 0:
            assume(corrected_amount > 0)
        return corrected_amount

    def correct_all_decimals(self, amounts: List[int]) -> list[int]:
        """Takes a list of amounts that use 18 decimals and reduces their
        precision to the number of decimals of the respective coins."""

        return [self.correct_decimals(a, i) for i, a in enumerate(amounts)]

    def get_balanced_deposit_amounts(self, amount: int):
        """Get the amounts of tokens that should be deposited
        to the pool to have balanced amounts of the two tokens.

        Args:
            amount (int): the amount of the first token

        Returns:
            List[int]: the amounts of the two tokens
        """
        return [int(amount), int(amount * 1e18 // self.pool.price_scale())]

    def report_equilibrium(self):
        """Helper function to report the current equilibrium of the pool.
        This is useful to see how the pool is doing in terms of
        imbalances.

        This is useful to see if a revert because of "unsafe values" in
        the math contract could be justified by the pool being too imbalanced.

        We compute the equilibrium as the ratio between the two tokens
        scaled prices. That is xp / yp where xp is the amount of the first
        token in the pool multiplied by its price scale (1) and yp is the
        amount of the second token in the pool multiplied by its price
        scale (price_scale).
        """
        # we calculate the equilibrium of the pool
        old_equilibrium = self.equilibrium

        # price of the first coin is always 1
        xp = self.coins[0].balanceOf(self.pool) * (
            10 ** (18 - self.decimals[0])  # normalize to 18 decimals
        )

        yp = (
            self.coins[1].balanceOf(self.pool)
            * self.pool.price_scale()  # price of the second coin
            * (10 ** (18 - self.decimals[1]))  # normalize to 18 decimals
        )

        self.equilibrium = xp * 1e18 / yp

        # we compute the percentage change from the old equilibrium
        # to have a sense of how much an operation changed the pool
        percentage_change = (
            self.equilibrium - old_equilibrium
        ) / old_equilibrium

        # we report equilibrium as log to make it easier to read
        note(
            "pool equilibrium {:.2f} (center is at 0) ".format(
                log10(self.equilibrium)
            )
            + "| change from old equilibrium: {:.4%}".format(percentage_change)
        )

    # --------------- pool methods ---------------
    # methods that wrap the pool methods that should be used in
    # the rules of the state machine. These methods make sure that
    # both the state of the pool and of the state machine are
    # updated together. Calling pool methods directly will probably
    # lead to incorrect simulation and errors.

    def add_liquidity(self, amounts: List[int], user: str):
        """Wrapper around the `add_liquidity` method of the pool.
        Always prefer this instead of calling the pool method directly
        when constructing rules.

        Args:
            amounts (List[int]): amounts of tokens to be deposited
            user (str): the sender of the transaction

        Returns:
            str: the address of the depositor
        """
        # check to prevent revert on empty deposits
        if sum(amounts) == 0:
            event("empty deposit")
            return

        for coin, amount in zip(self.coins, amounts):
            # infinite approval
            coin.approve(self.pool, 2**256 - 1, sender=user)
            # mint the amount of tokens for the depositor
            mint_for_testing(coin, user, amount)

        # store the amount of lp tokens before the deposit
        lp_tokens = self.pool.balanceOf(user)

        self.pool.add_liquidity(amounts, 0, sender=user)

        # find the increase in lp tokens
        lp_tokens = self.pool.balanceOf(user) - lp_tokens
        # increase the total supply by the amount of lp tokens
        self.total_supply += lp_tokens

        # pool balances should increase by the amounts
        self.balances = [x + y for x, y in zip(self.balances, amounts)]

        # update the profit since it increases through `tweak_price`
        # which is called by `add_liquidity`
        self.xcp_profit = self.pool.xcp_profit()
        self.xcp_profit_a = self.pool.xcp_profit_a()

        self.depositors.add(user)

    def exchange(self, dx: int, i: int, user: str) -> bool:
        """Wrapper around the `exchange` method of the pool.
        Always prefer this instead of calling the pool method directly
        when constructing rules.

        Args:
            dx (int): amount in
            i (int): the token the user sends to swap
            user (str): the sender of the transaction

        Returns:
            bool: True if the swap was successful, False otherwise
        """
        # j is the index of the coin that comes out of the pool
        j = 1 - i

        # mint coins for the user
        mint_for_testing(self.coins[i], user, dx)
        self.coins[i].approve(self.pool, dx, sender=user)

        note(
            "trying to swap {:.2e} of token {} ".format(
                dx,
                i,
            )
        )

        # store the balances of the user before the swap
        delta_balance_i = self.coins[i].balanceOf(user)
        delta_balance_j = self.coins[j].balanceOf(user)

        # depending on the pool state the swap might revert
        # because get_y hits some math
        try:
            expected_dy = self.pool.get_dy(i, j, dx)
        except boa.BoaError as e:
            # our top priority when something goes wrong is to
            # make sure that the lp can always withdraw their funds
            self.can_always_withdraw(imbalanced_operations_allowed=True)

            # we make sure that the revert was caused by the pool
            # being too imbalanced
            if e.stack_trace.last_frame.dev_reason.reason_str not in (
                "unsafe value for y",
                "unsafe values x[i]",
            ):
                raise ValueError(f"Reverted for the wrong reason: {e}")

            # we use the log10 of the equilibrium to obtain an easy interval
            # to work with. If the pool is balanced the equilibrium is 1 and
            # the log10 is 0.
            log_equilibrium = log10(self.equilibrium)
            # we store the old equilibrium to restore it after we make sure
            # that the pool can be healed
            event(
                "newton_y broke with log10 of x/y = {:.1f}".format(
                    log_equilibrium
                )
            )

            # we make sure that the pool is reasonably imbalanced
            assert (
                abs(log_equilibrium) >= 0.1
            ), "pool ({:.2e}) is not imbalanced".format(log_equilibrium)

            # we return False because the swap failed
            # (safe failure, but still a failure)
            return False

        # if get_y didn't fail we can safely swap
        actual_dy = self.pool.exchange(i, j, dx, expected_dy, sender=user)

        # compute the change in balances
        delta_balance_i = self.coins[i].balanceOf(user) - delta_balance_i
        delta_balance_j = self.coins[j].balanceOf(user) - delta_balance_j

        assert -delta_balance_i == dx, "didn't swap right amount of token x"
        assert (
            delta_balance_j == expected_dy == actual_dy
        ), "didn't receive the right amount of token y"

        # update the internal balances of the test for the invariants
        self.balances[i] -= delta_balance_i
        self.balances[j] -= delta_balance_j

        # update the profit made by the pool
        self.xcp_profit = self.pool.xcp_profit()

        self.swapped_once = True

        # we return True because the swap was successful
        return True

    def remove_liquidity(self, amount: int, user: str):
        """Wrapper around the `remove_liquidity` method of the pool.
        Always prefer this instead of calling the pool method directly
        when constructing rules.

        Args:
            amount (int): the amount of lp tokens to withdraw
            user (str): the address of the withdrawer
        """
        # store the balances of the user before the withdrawal
        amounts = [c.balanceOf(user) for c in self.coins]

        # withdraw the liquidity
        self.pool.remove_liquidity(amount, [0] * 2, sender=user)

        # compute the change in balances
        amounts = [
            (c.balanceOf(user) - a) for c, a in zip(self.coins, amounts)
        ]

        # total apply should have decreased by the amount of liquidity
        # withdrawn
        self.total_supply -= amount
        # update the internal balances of the test for the invariants
        self.balances = [b - a for a, b in zip(amounts, self.balances)]

        # we don't want to keep track of users with low liquidity because
        # it would approximate to 0 tokens and break the invariants.
        if self.pool.balanceOf(user) <= 1e0:
            self.depositors.remove(user)

        # virtual price resets if everything is withdrawn
        if self.total_supply == 0:
            event("full liquidity removal")
            self.virtual_price = 1e18

    def remove_liquidity_one_coin(
        self, percentage: float, coin_idx: int, user: str
    ):
        """Wrapper around the `remove_liquidity_one_coin` method of the pool.
        Always prefer this instead of calling the pool method directly
        when constructing rules.

        Args:
            percentage (float): percentage of liquidity to withdraw
            from the user balance
            coin_idx (int): index of the coin to withdraw
            user (str): address of the withdrawer
        """
        # when the fee receiver is the lp owner we can't compute the
        # balances in the invariants correctly. (This should never
        # be the case in production anyway).
        assume(user != self.fee_receiver)

        # store balances of the fee receiver before the removal
        admin_balances_pre = [
            c.balanceOf(self.fee_receiver) for c in self.coins
        ]
        # store the balance of the user before the removal
        user_balances_pre = self.coins[coin_idx].balanceOf(user)

        # lp tokens before the removal
        lp_tokens_balance_pre = self.pool.balanceOf(user)

        if percentage >= 0.99:
            # this corrects floating point errors that can lead to
            # withdrawing more than the user has
            lp_tokens_to_withdraw = lp_tokens_balance_pre
        else:
            lp_tokens_to_withdraw = int(lp_tokens_balance_pre * percentage)

        # this is a bit convoluted because we want this function
        # to continue in two scenarios:
        # 1. the function didn't revert (except block)
        # 2. the function reverted because the virtual price
        # decreased (try block + boa.reverts)
        try:
            with boa.reverts(dev="virtual price decreased"):
                self.pool.remove_liquidity_one_coin(
                    lp_tokens_to_withdraw,
                    coin_idx,
                    0,  # no slippage checks
                    sender=user,
                )
            # if we end up here something went wrong, so we need to check
            # if the pool was in a state that justifies a revert

            # we only allow small amounts to make the balance decrease
            # because of rounding errors
            assert (
                lp_tokens_to_withdraw < 1e16
            ), "virtual price decreased but but the amount was too high"
            event(
                "unsuccessful removal of liquidity because of "
                "loss (this should not happen too often)"
            )
            return
        except ValueError as e:
            assert str(e) == "Did not revert"
            # if the function didn't revert we can continue
            if lp_tokens_to_withdraw < 1e15:
                # useful to compare how often this happens compared to failures
                event("successful removal of liquidity with low amounts")

        # compute the change in balances
        user_balances_post = abs(
            user_balances_pre - self.coins[coin_idx].balanceOf(user)
        )

        # update internal balances
        self.balances[coin_idx] -= user_balances_post
        # total supply should decrease by the amount of tokens withdrawn
        self.total_supply -= lp_tokens_to_withdraw

        # we don't want to keep track of users with low liquidity because
        # it would approximate to 0 tokens and break the test.
        if self.pool.balanceOf(user) <= 1e0:
            self.depositors.remove(user)

        # invarinant upkeeping logic:
        # imbalanced removals can trigger a claim of admin fees

        # store the balances of the fee receiver after the removal
        new_xcp_profit_a = self.pool.xcp_profit_a()
        # store the balances of the fee receiver before the removal
        old_xcp_profit_a = self.xcp_profit_a

        # check if the admin fees were claimed (not always the case)
        if new_xcp_profit_a > old_xcp_profit_a:
            event("admin fees claim was detected")
            note("claiming admin fees during removal")
            # if the admin fees were claimed we have to update xcp
            self.xcp_profit_a = new_xcp_profit_a

            # store the balances of the fee receiver after the removal
            # (should be higher than before the removal)
            admin_balances_post = [
                c.balanceOf(self.fee_receiver) for c in self.coins
            ]

            for i in range(2):
                claimed_amount = admin_balances_post[i] - admin_balances_pre[i]
                note(
                    "admin received {:.2e} of token {}".format(
                        claimed_amount, i
                    )
                )
                assert (
                    claimed_amount > 0
                    # decimals: with such a low precision admin fees might be 0
                    or self.decimals[i] <= 4
                ), f"the admin fees collected should be positive for coin {i}"
                assert not self.is_ramping(), "claim admin fees while ramping"

                # deduce the claimed amount from the pool balances
                self.balances[i] -= claimed_amount

        # update test-tracked xcp profit
        self.xcp_profit = self.pool.xcp_profit()

    @rule(time_increase=integers(min_value=1, max_value=UNIX_DAY * 7))
    def time_forward(self, time_increase):
        """Make the time moves forward by `sleep_time` seconds.
        Useful for ramping, oracle updates, etc.
        Up to 1 week.
        """
        boa.env.time_travel(time_increase)

    # --------------- pool invariants ----------------------

    @invariant()
    def newton_y_converges(self):
        """We use get_dy with a small amount to check if the newton_y
        still manages to find the correct value. If this is not the case
        the pool is broken and it can't execute swaps anymore.
        """
        # TODO should this be even smaller? Or depend on the pool size?
        ARBITRARY_SMALL_AMOUNT = int(1e15)
        try:
            self.pool.get_dy(0, 1, ARBITRARY_SMALL_AMOUNT)
            try:
                self.pool.get_dy(1, 0, ARBITRARY_SMALL_AMOUNT)
            except Exception:
                raise AssertionError("newton_y is broken")
        except Exception:
            pass

    @invariant()
    def can_always_withdraw(self, imbalanced_operations_allowed=False):
        """Make sure that newton_D always works when withdrawing liquidity.
        No matter how imbalanced the pool is, it should always be possible
        to withdraw liquidity in a proportional way.
        """

        # anchor the environment to make sure that the balances are
        # restored after the invariant is checked
        with boa.env.anchor():
            # remove all liquidity from all depositors
            for d in self.depositors:
                # store the current balances of the pool
                prev_balances = [c.balanceOf(self.pool) for c in self.coins]
                # withdraw all liquidity from the depositor
                tokens = self.pool.balanceOf(d)
                self.pool.remove_liquidity(tokens, [0] * 2, sender=d)
                # assert current balances are less as the previous ones
                for c, b in zip(self.coins, prev_balances):
                    # check that the balance of the pool is less than before
                    if c.balanceOf(self.pool) == b:
                        assert self.pool.balanceOf(d) < 10, (
                            "balance of the depositor is not small enough to"
                            "justify a withdrawal that does not affect the"
                            "pool token balance"
                        )
                    else:
                        assert c.balanceOf(self.pool) < b, (
                            "one withdrawal didn't reduce the liquidity"
                            "of the pool"
                        )
            for c in self.coins:
                # there should not be any liquidity left in the pool
                assert (
                    # when imbalanced withdrawal occurs the pool protects
                    # itself by retaining some liquidity in the pool.
                    # In such a scenario a pool can have some liquidity left
                    # even after all withdrawals.
                    imbalanced_operations_allowed
                    or
                    # 1e7 is an arbitrary number that should be small enough
                    # not to worry about the pool actually not being empty.
                    c.balanceOf(self.pool) <= 1e7
                ), "pool still has signficant liquidity after all withdrawals"

    @invariant()
    def balances(self):
        balances = [self.pool.balances(i) for i in range(2)]
        balance_of = [c.balanceOf(self.pool) for c in self.coins]
        for i in range(2):
            assert (
                self.balances[i] == balances[i]
            ), "test-tracked balances don't match pool-tracked balances"
            assert (
                self.balances[i] == balance_of[i]
            ), "test-tracked balances don't match token-tracked balances"

    @invariant()
    def sanity_check(self):
        """Make sure the stateful simulations matches the contract state."""
        assert self.xcp_profit == self.pool.xcp_profit()
        assert self.total_supply == self.pool.totalSupply()

        # profit, cached vp and current vp should be at least 1e18
        assert self.xcp_profit >= 1e18, "profit should be at least 1e18"
        assert (
            self.pool.virtual_price() >= 1e18
        ), "cached virtual price should be at least 1e18"
        assert (
            self.pool.get_virtual_price() >= 1e18
        ), "virtual price should be at least 1e18"

        for d in self.depositors:
            assert (
                self.pool.balanceOf(d) > 0
            ), "tracked depositors should not have 0 lp tokens"

    @precondition(lambda self: self.swapped_once)
    @invariant()
    def virtual_price(self):
        assert (self.pool.virtual_price() - 1e18) * 2 >= (
            self.pool.xcp_profit() - 1e18
        ), "virtual price should be at least twice the profit"
        assert (
            abs(log(self.pool.virtual_price() / self.pool.get_virtual_price()))
            < 1e-10
        ), "cached virtual price shouldn't lag behind current virtual price"

    @invariant()
    def up_only_profit(self):
        """This method checks if the pool is profitable, since it should
        never lose money.

        To do so we use the so called `xcpx`. This is an empirical measure
        of profit that is even stronger than `xcp`. We have to use this
        because `xcp` goes down when claiming admin fees.

        You can imagine `xcpx` as a value that that is always between the
        interval [xcp_profit, xcp_profit_a]. When `xcp` goes down
        when claiming fees, `xcp_a` goes up. Averaging them creates this
        measure of profit that only goes down when something went wrong.
        """
        xcp_profit = self.pool.xcp_profit()
        xcp_profit_a = self.pool.xcp_profit_a()
        xcpx = (xcp_profit + xcp_profit_a + 1e18) // 2

        # make sure that the previous profit is smaller than the current
        assert xcpx >= self.xcpx, "xcpx has decreased"
        # updates the previous profit
        self.xcpx = xcpx
        self.xcp_profit = xcp_profit
        self.xcp_profit_a = xcp_profit_a


TestBase = StatefulBase.TestCase
