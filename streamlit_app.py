import streamlit as st
import psycopg2

# Connect to the Supabase Postgres database using the connection string saved in .streamlit/secrets.toml
def get_connection():
    return psycopg2.connect(st.secrets["DB_URL"])


# Run a SELECT query and return the rows as a list of dictionaries (so Streamlit can show them as a table)
def read_data(sql, params=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    columns = [col[0] for col in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# Run an INSERT / UPDATE / DELETE query, save the change, and show an error message if the database rejects it
def write_data(sql, params):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        st.error(f"Database error: {e}")
        return False


# Set the page title shown at the top of the app
st.title("Event Booking System")

# Sidebar menu: choose which table to work with and which action to perform
table = st.sidebar.radio("Table", ["Customers", "Bookings"])
action = st.sidebar.radio("Action", ["Insert", "Update", "Delete"])

# Load the dropdown choices from the lookup tables and the other tables (no JOINs are used in the app)
payment_methods = [row["payment_method_code"] for row in read_data("SELECT payment_method_code FROM ref_payment_methods ORDER BY payment_method_code")]
statuses = ["CONFIRMED", "PENDING", "CANCELLED", "EXPIRED"]  # must match the codes in ref_booking_statuses
customers = read_data("SELECT * FROM customers ORDER BY customer_id")
events = read_data("SELECT * FROM events ORDER BY event_id")
bookings = read_data("SELECT * FROM customer_bookings ORDER BY booking_id")

# Build lookup dictionaries so dropdowns show names instead of just ID numbers
customer_names = {c["customer_id"]: c["customer_name"] for c in customers}
event_names = {e["event_id"]: e["event_name"] for e in events}
customer_by_id = {c["customer_id"]: c for c in customers}
booking_by_id = {b["booking_id"]: b for b in bookings}


# ===================== CUSTOMERS =====================
if table == "Customers":
    st.header("Customers")

    # INSERT: form that adds a new customer row
    if action == "Insert":
        st.subheader("Add a new customer")
        name = st.text_input("Name")
        phone = st.text_input("Phone")
        email = st.text_input("Email")
        address = st.text_input("Address")
        method = st.selectbox("Payment method", payment_methods)

        # Save the new customer when the button is clicked
        if st.button("Add customer"):
            if name == "":
                st.warning("Name is required.")
            else:
                ok = write_data(
                    "INSERT INTO customers (customer_name, customer_phone, customer_email, customer_address, customer_payment_method) VALUES (%s, %s, %s, %s, %s)",
                    (name, phone, email, address, method),
                )
                if ok:
                    st.success("Customer added.")

    # UPDATE: pick an existing customer, edit the fields, and save
    elif action == "Update":
        st.subheader("Update a customer")
        cid = st.selectbox("Select customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
        current = customer_by_id[cid]
        name = st.text_input("Name", value=current["customer_name"])
        phone = st.text_input("Phone", value=current["customer_phone"] or "")
        email = st.text_input("Email", value=current["customer_email"] or "")
        address = st.text_input("Address", value=current["customer_address"] or "")
        method_index = payment_methods.index(current["customer_payment_method"]) if current["customer_payment_method"] in payment_methods else 0
        method = st.selectbox("Payment method", payment_methods, index=method_index)

        # Save the changes when the button is clicked
        if st.button("Update customer"):
            ok = write_data(
                "UPDATE customers SET customer_name=%s, customer_phone=%s, customer_email=%s, customer_address=%s, customer_payment_method=%s WHERE customer_id=%s",
                (name, phone, email, address, method, cid),
            )
            if ok:
                st.success("Customer updated.")

    # DELETE: pick a customer and require a confirmation checkbox before deleting
    elif action == "Delete":
        st.subheader("Delete a customer")
        cid = st.selectbox("Select customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
        confirm = st.checkbox("Yes, I am sure I want to delete this customer")

        # Only delete if the confirmation box is ticked
        if st.button("Delete customer"):
            if not confirm:
                st.warning("Please tick the confirmation box first.")
            else:
                ok = write_data("DELETE FROM customers WHERE customer_id=%s", (cid,))
                if ok:
                    st.success("Customer deleted.")

    # Show the current contents of the customers table (reloaded so changes appear right away)
    st.subheader("All customers")
    st.dataframe(read_data("SELECT * FROM customers ORDER BY customer_id"), use_container_width=True)


# ===================== BOOKINGS =====================
if table == "Bookings":
    st.header("Bookings")

    # INSERT: form that adds a new booking row
    if action == "Insert":
        st.subheader("Add a new booking")
        cid = st.selectbox("Customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
        eid = st.selectbox("Event", list(event_names.keys()), format_func=lambda i: f"{i} - {event_names[i]}")
        status = st.selectbox("Status", statuses)
        seats = st.number_input("Number of seats", min_value=1, value=1, step=1)

        # Save the new booking (today's date and the event's start time are filled in automatically)
        if st.button("Add booking"):
            ok = write_data(
                "INSERT INTO customer_bookings (customer_id, event_id, booking_status_code, event_datetime, booking_made_date, booking_seat_count) "
                "VALUES (%s, %s, %s, (SELECT event_start_date FROM events WHERE event_id = %s), CURRENT_DATE, %s)",
                (cid, eid, status, eid, int(seats)),
            )
            if ok:
                st.success("Booking added.")

    # UPDATE: pick an existing booking, change its status or seat count, and save
    elif action == "Update":
        st.subheader("Update a booking")
        bid = st.selectbox("Select booking", list(booking_by_id.keys()))
        current = booking_by_id[bid]
        status_index = statuses.index(current["booking_status_code"]) if current["booking_status_code"] in statuses else 0
        status = st.selectbox("Status", statuses, index=status_index)
        seats = st.number_input("Number of seats", min_value=1, value=int(current["booking_seat_count"]), step=1)

        # Save the changes when the button is clicked
        if st.button("Update booking"):
            ok = write_data(
                "UPDATE customer_bookings SET booking_status_code=%s, booking_seat_count=%s WHERE booking_id=%s",
                (status, int(seats), bid),
            )
            if ok:
                st.success("Booking updated.")

    # DELETE: pick a booking and require a confirmation checkbox before deleting
    elif action == "Delete":
        st.subheader("Delete a booking")
        bid = st.selectbox("Select booking", list(booking_by_id.keys()))
        confirm = st.checkbox("Yes, I am sure I want to delete this booking")

        # Only delete if the confirmation box is ticked
        if st.button("Delete booking"):
            if not confirm:
                st.warning("Please tick the confirmation box first.")
            else:
                ok = write_data("DELETE FROM customer_bookings WHERE booking_id=%s", (bid,))
                if ok:
                    st.success("Booking deleted.")

    # Show the current contents of the bookings table (reloaded so changes appear right away)
    st.subheader("All bookings")
    st.dataframe(read_data("SELECT * FROM customer_bookings ORDER BY booking_id"), use_container_width=True)
